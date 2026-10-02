"""Unattended updates: wake a host if it is asleep, update it, put it back as it was found.

The "as it was found" half is the point. A host that was off before the run is shut down after
it, so a weekly update sweep does not quietly leave a rack of on-demand machines running.
"""
import logging
import shlex
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from . import cancel, containers as container_module, membership, osinfo
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

    def to_dict(self) -> dict:
        return asdict(self)


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
                  containers_by_server: dict | None = None, cfg: dict | None = None,
                  defer: str | None = None) -> list[UpdateResult]:
    """Update one host and the VMs it starts. `defer` is a guest left for later: timar's own."""
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
            osinfo.note(ssh, host)
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
        if vm_name == defer:
            continue    # timar's own host; `run_updates` does it last, see `update_self`
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
                osinfo.note(ssh, vm_host)
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


# -- the host timar runs on --------------------------------------------------------------------
#
# Updating it from inside is how a run killed itself: the 2026-10-02 run upgraded docker-ce on
# that host as its first step, dockerd restarted, took this container with it twenty seconds in,
# and the run was recorded as "Interrupted" with the rest of the fleet untouched. Worse than the
# lost run, apt was a child of the SSH session the dying container held, so a dpkg run could be
# cut off half-way through by its own update. It finished that time; nothing made it.
#
# So the host goes last, its containers before its packages, and the package update is handed to
# the host's own systemd, started with a delay long enough for the run to be recorded first.

SELF_UNIT = "timar-self-update"
SELF_LOG = "/var/log/timar-self-update.log"
SELF_RC = "/var/log/timar-self-update.rc"

# Long enough to archive the report and send the notification before dockerd might restart.
# `AccuracySec=1s` matters as much as the number: a transient timer's default accuracy is one
# minute, and a 3-second timer measured on Debian 13 had not fired after six.
HANDOFF_DELAY = 60
POLL_INTERVAL = 15

_AS_ROOT = 'if [ "$(id -u)" = 0 ]; then S=; else S="sudo -n"; fi; '


def find_self_host(cfg: dict) -> str | None:
    """The configured server whose Docker runs this process, or None.

    Asked of the servers that have container projects registered, which is where timar's own
    project is listed: a host with no Docker on it cannot be running this container, and asking
    every machine would wake nothing but would still cost a connection to each of them.
    """
    me = container_module.self_container_id()
    if not me:
        return None
    servers_map = {s["name"]: s for s in cfg.get("servers", [])}
    candidates = dict.fromkeys(e.get("server") for e in cfg.get("containers") or [])
    for name in candidates:
        server = servers_map.get(name)
        if not server or not is_host_up(server["host"]):
            continue
        try:
            with connect(server["host"], server["user"], resolve_ssh_key(server)) as ssh:
                out, _, code = run(ssh, container_module.DOCKER
                                   + f"$D inspect --format '{{{{.Id}}}}' {me}", timeout=30)
        except Exception as e:
            logger.warning("could not ask %s whether timar runs there: %s", name, e)
            continue
        if code == 0 and out.strip() == me:
            return name
    logger.info("timar's own host was not found among the container hosts; config order is kept")
    return None


def self_update_command(update_cmd: str, unit: str) -> str:
    """The update command, wrapped to run under the host's systemd instead of this session.

    The exit status is written to a file rather than read back from systemd: `--collect` unloads
    the unit once it finishes (without it, a failed unit stays loaded and the next run cannot
    reuse the name), so there is nothing left to ask. Measured on Debian 13: nothing remains.
    """
    inner = (f"umask 022; ( {update_cmd} ) > {SELF_LOG} 2>&1; "
             f"echo $? > {SELF_RC}.tmp && mv {SELF_RC}.tmp {SELF_RC}")
    return (_AS_ROOT + f"$S rm -f {SELF_RC} && $S systemd-run --quiet --collect --unit={unit} "
            f"--on-active={HANDOFF_DELAY} --timer-property=AccuracySec=1s "
            f"/bin/sh -c {shlex.quote(inner)}")


def _self_update_status(ssh, unit: str) -> tuple[str, int | None, str]:
    """("done", rc, log tail), ("pending", None, "") or ("gone", None, "")."""
    out, _, _ = run(ssh, _AS_ROOT
                    + f"if [ -f {SELF_RC} ]; then echo done $(cat {SELF_RC}); $S tail -c 4000 {SELF_LOG}; "
                    f"elif systemctl list-units --all --plain --no-legend '{unit}.*' | grep -q .; "
                    f"then echo pending; else echo gone; fi", timeout=30)
    first, _, rest = out.partition("\n")
    word, _, code = first.strip().partition(" ")
    if word == "done":
        try:
            return "done", int(code), rest
        except ValueError:
            return "done", 1, rest
    return (word if word == "pending" else "gone"), None, ""


def wait_for_self_update(server_cfg: dict, unit: str, deadline: float,
                         note: str = "") -> UpdateResult:
    """Wait for the handed-off update to finish and report it like any other host.

    Each check is its own connection: the one the run started on may not survive what the
    update does, and the process asking may itself be a fresh one after a restart.
    """
    name = server_cfg["name"]
    while True:
        try:
            with connect(server_cfg["host"], server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
                status, rc, log = _self_update_status(ssh, unit)
        except Exception as e:
            logger.warning("%s: could not check the update yet: %s", name, e)
            status, rc, log = "pending", None, ""
        if status == "done":
            if rc == 0:
                return UpdateResult(server=name, success=True, note=note)
            return UpdateResult(server=name, success=False,
                                error=f"exit {rc}: {_tail(log) or 'no output'} (full log: {SELF_LOG})")
        if status == "gone":
            # Not started and not finished: the host restarted, or someone stopped the unit.
            return UpdateResult(server=name, success=False,
                                error=f"the update was handed to {unit} and never reported back")
        if time.time() > deadline:
            # Not killed, for the reason any other host's update is not: see DEFAULT_UPDATE_TIMEOUT.
            return UpdateResult(server=name, success=False,
                                error=f"still running when the time ran out — see {SELF_LOG}")
        time.sleep(POLL_INTERVAL)


def update_self(server_cfg: dict, entries: list[dict], done: list[UpdateResult],
                on_handoff=None) -> list[UpdateResult]:
    """Update timar's own host: its containers first, then its packages, handed off.

    `on_handoff(results, host, unit, deadline)` is told once the update is out of this
    process's hands, with everything finished so far, so that a restart that kills the run from
    here on can still finish its report instead of calling the whole run interrupted.
    """
    name = server_cfg["name"]
    platform = get_platform(server_cfg.get("platform"))
    update_cmd = server_cfg.get("update_cmd") or platform.default_update_cmd
    results: list[UpdateResult] = []
    try:
        with connect(server_cfg["host"], server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
            osinfo.note(ssh, server_cfg["host"])
            # Before the packages: a dockerd restart stops this process, and a container project
            # updated afterwards would be updated by nobody.
            results.extend(_update_containers(ssh, server_cfg, entries))
            if not update_cmd:
                return results + [UpdateResult(server=name, success=True, skipped=True,
                                               error=f"no update command configured for {platform.label}")]
            unit = f"{SELF_UNIT}-{datetime.now():%Y%m%d%H%M%S}"
            out, err, code = run(ssh, self_update_command(update_cmd, unit), timeout=60)
    except Exception as e:
        logger.exception("update %s", name)
        return results + [UpdateResult(server=name, success=False, error=str(e))]
    if code != 0:
        return results + [UpdateResult(server=name, success=False, error=failure_detail(out, err))]

    deadline = time.time() + HANDOFF_DELAY + _timeout_for(server_cfg)
    logger.info("Updating %s (timar's own host) through %s ...", name, unit)
    if on_handoff:
        on_handoff(done + results, name, unit, deadline)
    return results + [wait_for_self_update(server_cfg, unit, deadline)]


def run_updates(cfg, on_handoff=None) -> list[UpdateResult]:
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

    # Last, after everything that is woken and shut down again: the step that may restart this
    # process is then the only one left. Its hypervisor goes before it in the ordinary order,
    # which is safe because timar never reboots a host — upgraded hypervisor packages take effect
    # on a running VM only when it restarts. Rebooting hypervisors would change that.
    self_name = find_self_host(cfg)

    all_results = []
    for server in servers:
        if server["name"] in managed_vms or server["name"] == self_name:
            continue
        if cancel.requested("update"):
            logger.info("update run stopped by the operator before %s", server["name"])
            break
        # Not in the run: not woken, not connected to — and neither are the VMs it starts.
        if reason := membership.skip_reason(cfg, "update", server):
            all_results.append(UpdateResult(server=server["name"], success=True, skipped=True,
                                            was_running=False, error=reason))
            for vm in server.get("manages_vms", []):
                if vm["server_name"] == self_name:
                    continue    # reported once, below
                all_results.append(UpdateResult(server=vm["server_name"], success=True, skipped=True,
                                                was_running=False,
                                                error=f"its hypervisor {server['name']} is not in this run"))
            continue
        all_results.extend(update_server(server, servers_map, containers_by_server, cfg,
                                         defer=self_name))

    if self_name and not cancel.requested("update"):
        server = servers_map[self_name]
        if reason := membership.skip_reason(cfg, "update", server):
            all_results.append(UpdateResult(server=self_name, success=True, skipped=True, error=reason))
        else:
            all_results.extend(update_self(server, containers_by_server.get(self_name, []),
                                           all_results, on_handoff))

    return all_results
