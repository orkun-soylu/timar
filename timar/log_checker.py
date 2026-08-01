"""Read-only health sweep of a host: system log, disk, containers, scheduled-job logs.

Every command is chosen by the host's `Platform` (see platforms.py) rather than hardcoded, so
pointing this at an OpenWrt router does not silently produce an all-clear.
"""
import logging
from dataclasses import dataclass, field
from datetime import date

from .network import is_host_up
from .platforms import get as get_platform
from .config import resolve_ssh_key
from .ssh import connect, run

logger = logging.getLogger(__name__)

ERROR_KEYWORDS = ("error", "fail", "critical", "panic", "oom", "killed")


@dataclass
class JobLogResult:
    """Outcome of one scheduled job, read from the log it writes."""

    ran_today: bool
    completed: bool
    last_run: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.ran_today and self.completed and not self.errors


@dataclass
class LogResult:
    server: str
    success: bool
    offline: bool = False
    journal_errors: list = field(default_factory=list)
    disk_issues: list = field(default_factory=list)
    containers_stopped: list = field(default_factory=list)
    job_logs: dict = field(default_factory=dict)      # path -> JobLogResult
    log_file_issues: dict = field(default_factory=dict)  # path -> [lines]
    error: str = ""

    @property
    def has_issues(self) -> bool:
        return bool(
            self.journal_errors
            or self.disk_issues
            or self.containers_stopped
            or self.log_file_issues
            or any(not r.healthy for r in self.job_logs.values())
        )


def _check_job_log(ssh, path: str, started_marker: str, completed_marker: str) -> JobLogResult:
    """Did the job whose log this is start today, and did it finish?

    Watching for the *absence* of a run is the point: a backup that stops being scheduled
    produces no error line anywhere, so nothing but "it did not run today" will catch it.
    """
    today = date.today().strftime("%Y-%m-%d")

    started_out, _, _ = run(ssh, f"grep '{today}' {path} 2>/dev/null | grep -F {started_marker!r} | tail -1")
    completed_out, _, _ = run(ssh, f"grep '{today}' {path} 2>/dev/null | grep -F {completed_marker!r} | tail -1")

    def _timestamp(line: str) -> str | None:
        line = line.strip()
        if line.startswith("[") and "]" in line:
            return line[1:line.index("]")]
        return line[:19] or None

    if not started_out.strip():
        # Not today — report when it last ran at all, which is the number an operator needs.
        any_start, _, _ = run(ssh, f"grep -F {started_marker!r} {path} 2>/dev/null | tail -1")
        return JobLogResult(ran_today=False, completed=False, last_run=_timestamp(any_start))

    errors_out, _, _ = run(
        ssh,
        f"grep '{today}' {path} 2>/dev/null | grep -iE 'error|failed|critical' | tail -5",
    )
    return JobLogResult(
        ran_today=True,
        completed=bool(completed_out.strip()),
        last_run=_timestamp(started_out),
        errors=[l.strip()[:150] for l in errors_out.splitlines() if l.strip()],
    )


def _check_log_file(ssh, path: str) -> list[str]:
    stdout, stderr, rc = run(ssh, f"tail -100 {path} 2>&1")
    if rc != 0:
        return [f"[cannot read: {stderr.strip() or stdout.strip()}]"]
    hits = [
        line.strip()
        for line in stdout.splitlines()
        if any(kw in line.lower() for kw in ERROR_KEYWORDS)
    ]
    return hits[-20:]


def check_server(server_cfg: dict, hours: int = 6, disk_threshold: int = 85) -> LogResult:
    name = server_cfg["name"]
    host = server_cfg["host"]
    platform = get_platform(server_cfg.get("platform"))

    if not is_host_up(host):
        logger.info("%s is offline, skipping log check", name)
        return LogResult(server=name, success=True, offline=True)

    try:
        with connect(host, server_cfg["user"], resolve_ssh_key(server_cfg)) as ssh:
            journal_out, _, _ = run(ssh, platform.journal_cmd(hours))
            journal_errors = platform.parse_journal(journal_out)

            disk_out, _, _ = run(ssh, platform.disk_cmd())
            disk_issues = platform.parse_disk(disk_out, disk_threshold)

            containers_stopped = []
            if cmd := platform.docker_cmd():
                out, _, rc = run(ssh, cmd)
                if rc == 0:
                    containers_stopped = [l.strip() for l in out.splitlines() if l.strip()]

            job_logs = {}
            for job in server_cfg.get("job_logs", []):
                job_logs[job["path"]] = _check_job_log(
                    ssh, job["path"], job["started_marker"], job["completed_marker"]
                )

            log_file_issues = {}
            for path in server_cfg.get("watch_logs", []):
                if hits := _check_log_file(ssh, path):
                    log_file_issues[path] = hits

            return LogResult(
                server=name,
                success=True,
                journal_errors=journal_errors,
                disk_issues=disk_issues,
                containers_stopped=containers_stopped,
                job_logs=job_logs,
                log_file_issues=log_file_issues,
            )
    except Exception as e:
        logger.exception("log check failed for %s", name)
        return LogResult(server=name, success=False, error=str(e))


def run_log_checks(cfg: dict) -> list[LogResult]:
    defaults = cfg.get("log_check", {})
    hours = defaults.get("journal_hours", 6)
    threshold = defaults.get("disk_threshold", 85)

    results = []
    for server in cfg.get("servers", []):
        logger.info("Checking logs on %s", server["name"])
        results.append(check_server(server, hours, threshold))
    return results
