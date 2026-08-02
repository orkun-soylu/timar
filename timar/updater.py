"""Unattended updates: wake a host if it is asleep, update it, put it back as it was found.

The "as it was found" half is the point. A host that was off before the run is shut down after
it, so a weekly update sweep does not quietly leave a rack of on-demand machines running.
"""
import logging
import time
from dataclasses import dataclass

from .network import is_host_up, wait_for_host
from .platforms import get as get_platform
from .config import resolve_ssh_key
from .ssh import connect, run
from .wol import WolError, wake

logger = logging.getLogger(__name__)


@dataclass
class UpdateResult:
    server: str
    success: bool
    was_running: bool = True
    skipped: bool = False
    error: str = ""


# How long one host's update command may take. The old value was 300s, which is shorter than
# real work: a kernel upgrade that rebuilds a DKMS module, or an update command that also pulls
# container images, passes five minutes routinely. A 7 GB image alone can.
#
# Overshooting matters because of *how* the timeout fails. `run` hands it to paramiko as a
# channel read timeout, so nothing is sent to the far end — the remote command is not killed, it
# keeps running while Timar reports a failure it invented. The host is then left mid-upgrade and
# the next run meets a dpkg lock. It is also why the shutdown that would normally follow is
# skipped for this host: powering a machine off while apt is still writing is the one outcome
# worse than a late report.
#
# So the default is generous. Its real cost is that `run_updates` walks the fleet in sequence,
# and a genuinely wedged host delays the ones behind it by this much.
DEFAULT_UPDATE_TIMEOUT = 1800


def _timeout_for(server_cfg: dict) -> int:
    return int(server_cfg.get("update_timeout") or DEFAULT_UPDATE_TIMEOUT)


# Per stream, not per failure: keeping both is the whole point, and one of them is usually
# progress output that only earns its place by its last few lines.
TAIL = 400


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= TAIL else "..." + text[-TAIL:]


def failure_detail(stdout: str, stderr: str) -> str:
    """What to show for a command that exited non-zero.

    Both streams, labelled. Picking one and discarding the other cannot be right in either
    direction: an update command that touches containers always writes progress to stderr, so
    preferring stderr buries the line a wrapper script prints on stdout to say *which* service
    failed — and preferring stdout would bury an ordinary error message just as thoroughly.
    """
    parts = [f"{name}: {tail}"
             for name, tail in (("stdout", _tail(stdout)), ("stderr", _tail(stderr))) if tail]
    # A command can fail silently — a bare `exit 1`, or output swallowed by a redirect. Saying so
    # is worth a line, because the alternative is an empty red mark that reads like a Timar bug.
    return "\n".join(parts) or "the command failed without printing anything"


def _do_update(ssh, cmd: str, timeout: int = DEFAULT_UPDATE_TIMEOUT):
    stdout, stderr, rc = run(ssh, cmd, timeout=timeout)
    if rc != 0:
        return False, failure_detail(stdout, stderr)
    return True, ""


def _wake_and_wait(server_cfg, servers_map: dict | None = None) -> bool:
    name = server_cfg["name"]
    logger.info("%s is offline, sending a magic packet ...", name)
    try:
        wake(server_cfg, servers_map)
    except WolError as e:
        # Distinguished from "did not come up": a packet that could not be sent is an operator
        # problem (missing MAC, unreachable relay), while a packet sent to a machine that stays
        # dark is usually Wake-on-LAN disabled in its firmware. Same outcome, different fix.
        logger.error("%s: %s", name, e)
        return False

    if not wait_for_host(server_cfg["host"]):
        logger.error("%s did not come up after the magic packet was sent", name)
        return False
    logger.info("%s is up", name)
    return True


def _shutdown_host(ssh, platform, user: str = "root"):
    try:
        run(ssh, platform.shutdown_cmd(user), timeout=10)
    except Exception:
        pass  # connection drops as it shuts down


def _wait_offline(host: str, max_wait: int = 120):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if not is_host_up(host):
            return
        time.sleep(5)


def update_server(server_cfg: dict, servers_map: dict) -> list[UpdateResult]:
    name = server_cfg["name"]
    host = server_cfg["host"]
    platform = get_platform(server_cfg.get("platform"))
    results = []

    # Resolved before the host is woken: a platform with no safe default (OpenWrt) and no
    # operator-supplied command has nothing to do, and waking a machine to do nothing is worse
    # than useless — on a router it is a needless reboot risk.
    update_cmd = server_cfg.get("update_cmd") or platform.default_update_cmd
    if not update_cmd:
        logger.info("%s: no update command for platform %r, skipping", name, platform.id)
        return [UpdateResult(server=name, success=True, skipped=True,
                             error=f"no update command configured for {platform.label}")]

    was_running = is_host_up(host)

    if not was_running:
        if not _wake_and_wait(server_cfg, servers_map):
            return [UpdateResult(server=name, success=False, was_running=False,
                                 error="Host did not come up after WOL")]

    try:
        with connect(host, server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
            logger.info("Updating %s ...", name)
            ok, err = _do_update(ssh, update_cmd, timeout=_timeout_for(server_cfg))
            results.append(UpdateResult(server=name, success=ok, was_running=was_running,
                                        error=err if not ok else ""))
    except Exception as e:
        logger.exception("update %s", name)
        results.append(UpdateResult(server=name, success=False, was_running=was_running, error=str(e)))
        return results

    # handle VMs managed by this host
    for vm_entry in server_cfg.get("manages_vms", []):
        vm_id = vm_entry["vm_id"]
        vm_name = vm_entry["server_name"]
        vm_cfg = servers_map.get(vm_name)
        if not vm_cfg:
            logger.warning("VM %s not found in servers config", vm_name)
            continue

        vm_host = vm_cfg["host"]
        vm_platform = get_platform(vm_cfg.get("platform"))

        # Resolved before `qm start`, for the same reason as the host above: if there is nothing
        # to run, the VM must not be booted at all. Deciding this after the boot would also have
        # to unwind it, and the early return that skipped the update would skip the shutdown too.
        vm_update_cmd = vm_cfg.get("update_cmd") or vm_platform.default_update_cmd
        if not vm_update_cmd:
            logger.info("VM %s: no update command for platform %r, skipping",
                        vm_name, vm_platform.id)
            results.append(UpdateResult(
                server=vm_name, success=True, skipped=True, was_running=is_host_up(vm_host),
                error=f"no update command configured for {vm_platform.label}"))
            continue

        vm_was_running = is_host_up(vm_host)

        if not vm_was_running:
            logger.info("Starting VM %s (id=%s) on %s ...", vm_name, vm_id, name)
            try:
                with connect(host, server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
                    run(ssh, f"qm start {vm_id}", timeout=30)
                if not wait_for_host(vm_host):
                    results.append(UpdateResult(server=vm_name, success=False, was_running=False,
                                                error="VM did not come up after qm start"))
                    continue
            except Exception as e:
                results.append(UpdateResult(server=vm_name, success=False, was_running=False, error=str(e)))
                continue

        try:
            with connect(vm_host, vm_cfg["user"], resolve_ssh_key(vm_cfg)) as ssh:
                logger.info("Updating VM %s ...", vm_name)
                ok, err = _do_update(ssh, vm_update_cmd, timeout=_timeout_for(vm_cfg))
                results.append(UpdateResult(server=vm_name, success=ok, was_running=vm_was_running,
                                            error=err if not ok else ""))
        except Exception as e:
            logger.exception("update vm %s", vm_name)
            results.append(UpdateResult(server=vm_name, success=False,
                                        was_running=vm_was_running, error=str(e)))
            continue

        if not vm_was_running:
            logger.info("Shutting down VM %s ...", vm_name)
            try:
                with connect(host, server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
                    run(ssh, f"qm shutdown {vm_id}", timeout=60)
                _wait_offline(vm_host)
                logger.info("VM %s is down", vm_name)
            except Exception as e:
                logger.warning("Could not shutdown VM %s: %s", vm_name, e)

    if not was_running:
        logger.info("Shutting down %s (was offline before update) ...", name)
        try:
            with connect(host, server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
                _shutdown_host(ssh, platform, user=server_cfg["user"])
            _wait_offline(host)
            logger.info("%s is down", name)
        except Exception as e:
            logger.warning("Could not shutdown %s: %s", name, e)

    return results


def run_updates(cfg) -> list[UpdateResult]:
    servers = cfg.get("servers", [])
    servers_map = {s["name"]: s for s in servers}

    # collect VM names so we don't update them directly (handled by their host)
    managed_vms = set()
    for s in servers:
        for vm in s.get("manages_vms", []):
            managed_vms.add(vm["server_name"])

    all_results = []
    for server in servers:
        if server["name"] in managed_vms:
            continue
        all_results.extend(update_server(server, servers_map))

    return all_results
