"""Powering one machine on or off, on purpose, right now.

The scheduled work already wakes and shuts down machines around an update run. This is the same
pair of operations exposed as a button, which is a different problem in two ways:

- **The two kinds of machine are powered differently.** A physical host wakes on a magic packet
  and shuts down over SSH. A guest has no wake address of its own — it cannot have one, it is
  started by `qm` — so both directions go through its hypervisor. Keying off `wol_mac` alone
  would offer a guest a wake button that always answers "no MAC address configured".
- **A person is waiting for the answer.** Every failure here has to come back as a sentence the
  operator can act on, not as a stack trace or a silence, because the machine going dark is also
  what success looks like.

`shutdown` refuses a machine Timar cannot wake again. That rule is what keeps the button from
stranding a host: powering off an always-on machine over its own SSH connection works perfectly
and leaves nothing to bring it back but a walk to the rack.
"""
from __future__ import annotations

import logging

from . import config, wol
from .network import is_host_up
from .platforms import get as get_platform
from .ssh import connect, run

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15

# How long `qm` waits for the guest to power off. Deliberately bounded and deliberately *not*
# `--forceStop`: a guest that ignores ACPI needs its operator, not the equivalent of pulling its
# power cord. When it expires, `qm` exits non-zero and that becomes the message on the page.
GUEST_SHUTDOWN_TIMEOUT = 30


class PowerError(RuntimeError):
    """A failure with something an operator can do about it in the message."""


def guest_link(name: str, servers: list[dict]) -> tuple[dict, int] | None:
    """The hypervisor responsible for `name` and the VM id it knows it by, if it is a guest."""
    for host in servers:
        for guest in host.get("manages_vms", []):
            if guest.get("server_name") == name:
                return host, int(guest["vm_id"])
    return None


def _on_hypervisor(hypervisor: dict, command: str, timeout: int) -> None:
    """Run a `qm` command on the machine that owns the guest.

    The reachability check is not redundant with the connect below: "could not reach
    pve-prod-01" is a paramiko error message, while "pve-prod-01 is offline — wake it first"
    names the actual next action. Starting a guest on a sleeping hypervisor is the ordinary
    mistake here, not an exotic one.
    """
    if not is_host_up(hypervisor["host"]):
        raise PowerError(f"{hypervisor['name']} is offline — wake it first, then try again")
    try:
        with connect(hypervisor["host"], hypervisor["user"],
                     config.resolve_ssh_key(hypervisor), timeout=CONNECT_TIMEOUT) as ssh:
            stdout, stderr, code = run(ssh, command, timeout=timeout)
    except Exception as e:
        raise PowerError(f"could not reach {hypervisor['name']}: {e}") from e
    if code != 0:
        detail = (stderr.strip() or stdout.strip() or "the command failed without printing "
                                                      "anything")
        raise PowerError(f"{hypervisor['name']}: {detail[:200]}")


def wake(server: dict, servers: list[dict]) -> str:
    """Bring `server` up. Returns what was done, for the operator to read."""
    link = guest_link(server["name"], servers)
    if link:
        hypervisor, vm_id = link
        _on_hypervisor(hypervisor, f"qm start {vm_id}", timeout=60)
        logger.info("started %s (vm %s) on %s", server["name"], vm_id, hypervisor["name"])
        return f"{server['name']} started on {hypervisor['name']}"

    try:
        wol.wake(server, {s["name"]: s for s in servers})
    except wol.WolError as e:
        raise PowerError(str(e)) from e
    via = f" via {server['wol_relay']}" if server.get("wol_relay") else ""
    return f"magic packet sent to {server['name']}{via}"


def shutdown(server: dict, servers: list[dict]) -> str:
    """Power `server` off, refusing any machine Timar has no way to wake again."""
    name = server["name"]
    if name not in config.on_demand(servers):
        raise PowerError(
            f"{name} is always on — Timar will not shut down a machine it cannot wake again. "
            "Give it a MAC address, or a hypervisor, first."
        )

    link = guest_link(name, servers)
    if link:
        hypervisor, vm_id = link
        _on_hypervisor(hypervisor, f"qm shutdown {vm_id} --timeout {GUEST_SHUTDOWN_TIMEOUT}",
                       timeout=GUEST_SHUTDOWN_TIMEOUT + 15)
        logger.info("shut down %s (vm %s) via %s", name, vm_id, hypervisor["name"])
        return f"{name} shut down via {hypervisor['name']}"

    platform = get_platform(server.get("platform"))
    user = server["user"]

    # `connected` separates the two failures that look alike from the outside. Losing the
    # connection *after* the command was accepted is what a successful shutdown looks like —
    # the machine drops the link on its way down — while losing it before is an unreachable
    # host, and reporting that as "shutting down" would leave a machine running and an
    # operator believing otherwise.
    connected = False
    try:
        with connect(server["host"], user, config.resolve_ssh_key(server),
                     timeout=CONNECT_TIMEOUT) as ssh:
            connected = True
            stdout, stderr, code = run(ssh, platform.shutdown_cmd(user), timeout=15)
    except Exception as e:
        if connected:
            logger.info("%s dropped the connection while shutting down", name)
            return f"{name} is shutting down"
        raise PowerError(f"could not reach {name}: {e}") from e

    if code != 0:
        # Almost always sudo: an account without passwordless sudo cannot halt its own machine,
        # and the refusal is silent unless it is repeated here.
        detail = stderr.strip() or stdout.strip() or "the command failed without printing anything"
        raise PowerError(f"{name} refused the shutdown: {detail[:200]}")

    logger.info("%s is shutting down", name)
    return f"{name} is shutting down"
