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

**A guest on a sleeping hypervisor is woken through it.** The operator pressed wake on the
guest, and what they want is the guest running; telling them to wake the hypervisor first and
come back is a chore Timar can do. It is slow — the hypervisor boots, then starts its own
`onboot` guests — so it runs in the background and the dashboard shows the row as waking. A
hypervisor Timar cannot wake gives its guests no wake button at all (`status`).

`shutdown` works on every machine Timar can reach, including ones it cannot wake again — an
always-on server, a board switched on by hand. It used to refuse those, on the grounds that
nothing but a walk to the rack brings them back; but that is the operator's call to make, and
the dashboard makes it with them: the confirmation for such a machine says it will stay off.
"""
from __future__ import annotations

import logging
import threading
import time

from . import config, wol
from .i18n import gettext as _
from .network import is_host_up, wait_for_host
from .platforms import get as get_platform
from .ssh import connect, run

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15

# How long `qm` waits for the guest to power off. Deliberately bounded and deliberately *not*
# `--forceStop`: a guest that ignores ACPI needs its operator, not the equivalent of pulling its
# power cord. When it expires, `qm` exits non-zero and that becomes the message on the page.
GUEST_SHUTDOWN_TIMEOUT = 30

# How long a freshly woken Proxmox host may take to start its `onboot` guests. `pve-guests` is a
# oneshot that stays active once it is done, so "active" means every boot-time start is behind
# us. Bounded because a guest that hangs at start must not hold the chain forever — past this,
# `qm start` is tried anyway and reports whatever it finds.
GUESTS_SETTLE_TIMEOUT = 300
SETTLE_CMD = (f"timeout {GUESTS_SETTLE_TIMEOUT} sh -c 'until s=$(systemctl is-active pve-guests);"
              f" [ \"$s\" = active ] || [ \"$s\" = failed ]; do sleep 2; done'")

# A failed background wake stays on its row this long, then the row is as it was. Long enough to
# be seen by someone who looked away while the hypervisor booted.
FAILURE_SHOWN = 600

# Chained wakes in flight, and the ones that failed. Read by every dashboard poll, written by the
# background threads, hence the lock. In memory on purpose: a restart ends the threads too.
_lock = threading.Lock()
_waking: set[str] = set()
_failed: dict[str, tuple[float, str]] = {}


class PowerError(RuntimeError):
    """A failure with something an operator can do about it in the message."""


def guest_link(name: str, servers: list[dict]) -> tuple[dict, int] | None:
    """The hypervisor responsible for `name` and the VM id it knows it by, if it is a guest."""
    for host in servers:
        for guest in host.get("manages_vms", []):
            if guest.get("server_name") == name:
                return host, int(guest["vm_id"])
    return None


def _on_hypervisor(hypervisor: dict, command: str, timeout: int) -> tuple[str, str]:
    """Run a `qm` command on the machine that owns the guest; its output on success.

    The reachability check is not redundant with the connect below: "could not reach
    pve-prod-01" is a paramiko error message, while "pve-prod-01 is offline" names the actual
    state. `wake` never gets here with a sleeping hypervisor — it wakes it first — so this is the
    shutdown path, and a hypervisor that dropped between the dashboard's poll and the click.
    """
    if not is_host_up(hypervisor["host"]):
        raise PowerError(_("{name} is offline", name=hypervisor["name"]))
    try:
        with connect(hypervisor["host"], hypervisor["user"],
                     config.resolve_ssh_key(hypervisor), timeout=CONNECT_TIMEOUT) as ssh:
            stdout, stderr, code = run(ssh, command, timeout=timeout)
    except Exception as e:
        raise PowerError(_("could not reach {name}: {error}", name=hypervisor["name"], error=e)) from e
    if code != 0:
        detail = stderr.strip() or stdout.strip() or _("the command failed without printing anything")
        raise PowerError(f"{hypervisor['name']}: {detail[:200]}")
    return stdout, stderr


def _start_guest(server: dict, hypervisor: dict, vm_id: int) -> str:
    """`qm start`, unless the guest is already running — then that is the answer.

    Asked first because a hypervisor that has just booted has started its `onboot` guests on its
    own, and `qm start` on a running VM is an error that would report success as a failure.
    """
    stdout, _err = _on_hypervisor(hypervisor, f"qm status {vm_id}", timeout=30)
    if "status: running" in stdout:
        return _("{name} is already running on {hypervisor}",
                 name=server["name"], hypervisor=hypervisor["name"])
    _on_hypervisor(hypervisor, f"qm start {vm_id}", timeout=60)
    logger.info("started %s (vm %s) on %s", server["name"], vm_id, hypervisor["name"])
    return _("{name} started on {hypervisor}", name=server["name"], hypervisor=hypervisor["name"])


def _wake_through(server: dict, hypervisor: dict, vm_id: int, servers: list[dict]) -> str:
    """Wake the hypervisor, wait for it and its boot-time guests, then start the guest.

    Blocking, minutes long; `wake` runs it on a thread of its own.
    """
    wake(hypervisor, servers, background=False)
    if not wait_for_host(hypervisor["host"]):
        raise PowerError(_("{hypervisor} did not come up — {name} was not started",
                           hypervisor=hypervisor["name"], name=server["name"]))
    try:
        with connect(hypervisor["host"], hypervisor["user"],
                     config.resolve_ssh_key(hypervisor), timeout=CONNECT_TIMEOUT) as ssh:
            run(ssh, SETTLE_CMD, timeout=GUESTS_SETTLE_TIMEOUT + 15)
    except Exception as e:
        # Not fatal: the guest may start regardless, and `_start_guest` says if it cannot.
        logger.warning("%s: could not wait for its boot-time guests: %s", hypervisor["name"], e)
    return _start_guest(server, hypervisor, vm_id)


def _run_chain(server: dict, hypervisor: dict, vm_id: int, servers: list[dict]) -> None:
    names = {server["name"], hypervisor["name"]}
    try:
        logger.info("%s: %s", server["name"], _wake_through(server, hypervisor, vm_id, servers))
    except Exception as e:
        message = str(e) if isinstance(e, PowerError) else _(
            "waking {name} failed: {error}", name=server["name"], error=e)
        logger.error("%s", message)
        with _lock:
            _failed[server["name"]] = (time.monotonic(), message)
    finally:
        with _lock:
            _waking.difference_update(names)


def waking() -> set[str]:
    """The machines a chained wake is working on now — the guest and its hypervisor."""
    with _lock:
        return set(_waking)


def failures() -> dict[str, str]:
    """Recent chained wakes that failed, by guest, for the row that has to say so."""
    now = time.monotonic()
    with _lock:
        for name in [n for n, (at, _m) in _failed.items() if now - at > FAILURE_SHOWN]:
            del _failed[name]
        return {name: message for name, (_at, message) in _failed.items()}


def wake(server: dict, servers: list[dict], background: bool = True) -> str:
    """Bring `server` up. Returns what was done, for the operator to read.

    A guest whose hypervisor is asleep wakes the hypervisor first. With `background` that runs
    on a thread and this returns at once; the row shows it waking.
    """
    link = guest_link(server["name"], servers)
    if link:
        hypervisor, vm_id = link
        if is_host_up(hypervisor["host"]):
            return _start_guest(server, hypervisor, vm_id)
        if not config.can_wake(hypervisor["name"], servers):
            raise PowerError(_("{hypervisor} is off and timar cannot wake it — "
                               "{name} can start only once it is switched on",
                               hypervisor=hypervisor["name"], name=server["name"]))
        if not background:
            return _wake_through(server, hypervisor, vm_id, servers)
        names = {server["name"], hypervisor["name"]}
        with _lock:
            if names & _waking:
                return _("{name} is already being woken", name=server["name"])
            _waking.update(names)
            _failed.pop(server["name"], None)
        threading.Thread(target=_run_chain, args=(server, hypervisor, vm_id, servers),
                         name=f"wake-{server['name']}", daemon=True).start()
        return _("waking {hypervisor} first — {name} starts once it is up",
                 hypervisor=hypervisor["name"], name=server["name"])

    try:
        wol.wake(server, {s["name"]: s for s in servers})
    except wol.WolError as e:
        raise PowerError(str(e)) from e
    if relay := server.get("wol_relay"):
        return _("magic packet sent to {name} via {relay}", name=server["name"], relay=relay)
    return _("magic packet sent to {name}", name=server["name"])


def shutdown(server: dict, servers: list[dict]) -> str:
    """Power `server` off — a guest through its hypervisor, anything else over its own SSH."""
    name = server["name"]
    link = guest_link(name, servers)
    if link:
        hypervisor, vm_id = link
        _on_hypervisor(hypervisor, f"qm shutdown {vm_id} --timeout {GUEST_SHUTDOWN_TIMEOUT}",
                       timeout=GUEST_SHUTDOWN_TIMEOUT + 15)
        logger.info("shut down %s (vm %s) via %s", name, vm_id, hypervisor["name"])
        return _("{name} shut down via {hypervisor}", name=name, hypervisor=hypervisor["name"])
    if not config.has_ssh(server):
        # The page offers no button for it; this refuses a hand-made request the same way.
        raise PowerError(_("{name} is watched only — timar has no SSH to it", name=name))

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
            return _("{name} is shutting down", name=name)
        raise PowerError(_("could not reach {name}: {error}", name=name, error=e)) from e

    if code != 0:
        # Almost always sudo: an account without passwordless sudo cannot halt its own machine,
        # and the refusal is silent unless it is repeated here.
        detail = stderr.strip() or stdout.strip() or _("the command failed without printing anything")
        raise PowerError(_("{name} refused the shutdown: {detail}", name=name, detail=detail[:200]))

    logger.info("%s is shutting down", name)
    return _("{name} is shutting down", name=name)
