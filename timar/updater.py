"""Unattended updates: wake a host if it is asleep, update it, put it back as it was found.

The "as it was found" half is the point. A host that was off before the run is shut down after
it, so a weekly update sweep does not quietly leave a rack of on-demand machines running.
"""
import logging
import time
from dataclasses import dataclass

from . import cancel, containers as container_module, membership
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
    note: str = ""      # a word about how it went, shown after the name — "left stopped"


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


def _update_containers(ssh, server_cfg: dict, entries: list[dict]) -> list[UpdateResult]:
    """Update the compose projects registered on this host, one result each.

    Run on the connection the host's own update used, right after it: the host is up and
    reachable now, and for a host that was woken for the run, now is the only time it is.
    """
    if not entries:
        return []
    host_name = server_cfg["name"]
    out, err, code = run(ssh, container_module.DOCKER + "$D ps -a --no-trunc --format json", timeout=60)
    if code != 0:
        return [UpdateResult(server=f"containers on {host_name}", success=False,
                             error=(err.strip() or out.strip() or "docker ps failed")[-300:])]
    by_dir = container_module.parse_ps(out)
    me = container_module.self_container_id()

    results = []
    for entry in entries:
        if cancel.requested("update"):
            break
        label = f"{entry['name']} ({host_name})"
        found = by_dir.get(entry["path"].rstrip("/"), [])
        if me and any(c.id == me for c in found):
            results.append(UpdateResult(server=label, success=True, skipped=True,
                                        error="timar itself — updated by its own release, not from inside"))
            continue
        if entry.get("update") == "skip":
            results.append(UpdateResult(server=label, success=True, skipped=True, error="update: skip"))
            continue
        running = any(c.state in ("running", "restarting") for c in found)
        logger.info("Updating container project %s ...", label)
        try:
            ok, err = _do_update(ssh, container_module.update_command(entry, running),
                                 timeout=_timeout_for(entry))
        except Exception as e:
            ok, err = False, str(e)
        note = "" if running or entry.get("update") == "custom" else "pulled, left stopped"
        results.append(UpdateResult(server=label, success=ok, error="" if ok else err, note=note))

    # Best effort: an image the pulls replaced is now unused, and a failure to tidy up is not a
    # failure of the run.
    try:
        run(ssh, container_module.DOCKER + "$D image prune -f", timeout=300)
    except Exception:
        logger.warning("image prune on %s failed", host_name)
    return results


def update_server(server_cfg: dict, servers_map: dict,
                  containers_by_server: dict | None = None, cfg: dict | None = None) -> list[UpdateResult]:
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

    # Off on purpose with nothing to wake it: skipped, not failed. Trying the magic packet anyway
    # fails on the missing MAC, and a red mark every week for a machine that is exactly where it
    # was left is how an operator learns to stop reading the report.
    if not was_running and server_cfg.get("on_demand") and not server_cfg.get("wol_mac"):
        logger.info("%s is off and is switched on by hand, skipping", name)
        return [UpdateResult(server=name, success=True, skipped=True, was_running=False,
                             error="off — switched on by hand, not woken")]

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
            results.extend(_update_containers(ssh, server_cfg, (containers_by_server or {}).get(name, [])))
    except Exception as e:
        logger.exception("update %s", name)
        results.append(UpdateResult(server=name, success=False, was_running=was_running, error=str(e)))
        return results

    # handle VMs managed by this host
    for vm_entry in server_cfg.get("manages_vms", []):
        if cancel.requested("update"):
            break       # still falls through to shutting this host down if it was woken
        vm_id = vm_entry["vm_id"]
        vm_name = vm_entry["server_name"]
        vm_cfg = servers_map.get(vm_name)
        if not vm_cfg:
            logger.warning("VM %s not found in servers config", vm_name)
            continue
        if cfg is not None and (reason := membership.skip_reason(cfg, "update", vm_cfg)):
            results.append(UpdateResult(server=vm_name, success=True, skipped=True,
                                        was_running=False, error=reason))
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
                results.extend(_update_containers(ssh, vm_cfg, (containers_by_server or {}).get(vm_name, [])))
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

    containers_by_server: dict[str, list[dict]] = {}
    for entry in cfg.get("containers") or []:
        containers_by_server.setdefault(entry.get("server"), []).append(entry)

    all_results = []
    for server in servers:
        if server["name"] in managed_vms:
            continue
        if cancel.requested("update"):
            logger.info("update run stopped by the operator before %s", server["name"])
            break
        # Not in the run: not woken, not connected to — and neither are the VMs it starts.
        if reason := membership.skip_reason(cfg, "update", server):
            all_results.append(UpdateResult(server=server["name"], success=True, skipped=True,
                                            was_running=False, error=reason))
            for vm in server.get("manages_vms", []):
                all_results.append(UpdateResult(server=vm["server_name"], success=True, skipped=True,
                                                was_running=False,
                                                error=f"its hypervisor {server['name']} is not in this run"))
            continue
        all_results.extend(update_server(server, servers_map, containers_by_server, cfg))

    return all_results
