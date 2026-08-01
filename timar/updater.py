"""Unattended updates: wake a host if it is asleep, update it, put it back as it was found.

The "as it was found" half is the point. A host that was off before the run is shut down after
it, so a weekly update sweep does not quietly leave a rack of on-demand machines running.
"""
import logging
import time
from dataclasses import dataclass

from .network import is_host_up, wait_for_host
from .platforms import get as get_platform
from .ssh import connect, run
from .wol import send_magic_packet

logger = logging.getLogger(__name__)


@dataclass
class UpdateResult:
    server: str
    success: bool
    was_running: bool = True
    skipped: bool = False
    error: str = ""


def _do_update(ssh, cmd: str, timeout: int = 300):
    stdout, stderr, rc = run(ssh, cmd, timeout=timeout)
    if rc != 0:
        return False, (stderr or stdout)[-500:]
    return True, ""


def _wake_and_wait(server_cfg) -> bool:
    name = server_cfg["name"]
    mac = server_cfg.get("wol_mac")
    broadcast = server_cfg.get("wol_broadcast", "255.255.255.255")
    if not mac:
        logger.error("%s: wol_mac not configured", name)
        return False
    logger.info("%s is offline, sending WOL to %s ...", name, mac)
    send_magic_packet(mac, broadcast)
    if not wait_for_host(server_cfg["host"]):
        logger.error("%s did not come up after WOL", name)
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
        if not _wake_and_wait(server_cfg):
            return [UpdateResult(server=name, success=False, was_running=False,
                                 error="Host did not come up after WOL")]

    try:
        with connect(host, server_cfg["user"], server_cfg["ssh_key"]) as ssh:
            logger.info("Updating %s ...", name)
            ok, err = _do_update(ssh, update_cmd)
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
                with connect(host, server_cfg["user"], server_cfg["ssh_key"]) as ssh:
                    run(ssh, f"qm start {vm_id}", timeout=30)
                if not wait_for_host(vm_host):
                    results.append(UpdateResult(server=vm_name, success=False, was_running=False,
                                                error="VM did not come up after qm start"))
                    continue
            except Exception as e:
                results.append(UpdateResult(server=vm_name, success=False, was_running=False, error=str(e)))
                continue

        try:
            with connect(vm_host, vm_cfg["user"], vm_cfg["ssh_key"]) as ssh:
                logger.info("Updating VM %s ...", vm_name)
                ok, err = _do_update(ssh, vm_update_cmd)
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
                with connect(host, server_cfg["user"], server_cfg["ssh_key"]) as ssh:
                    run(ssh, f"qm shutdown {vm_id}", timeout=60)
                _wait_offline(vm_host)
                logger.info("VM %s is down", vm_name)
            except Exception as e:
                logger.warning("Could not shutdown VM %s: %s", vm_name, e)

    if not was_running:
        logger.info("Shutting down %s (was offline before update) ...", name)
        try:
            with connect(host, server_cfg["user"], server_cfg["ssh_key"]) as ssh:
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
