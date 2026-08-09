"""Job history and liveness, persisted so a restart does not erase what happened.

This file exists because of one failure mode. Timar's predecessor lost its scheduled update job
during a host migration: the trigger silently disappeared, the service kept reporting itself as
running, and nobody noticed for two weeks. Nothing was broken in a way anything could see —
which is the point. **A job that stops being scheduled produces no error, no output, and no
signal of any kind.** Only a visible "last run" timestamp reveals it.

So the dashboard shows, for every job: when it last ran, whether it succeeded, and when it is
due next. If those are stale, something is wrong even when everything looks healthy.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from . import config

logger = logging.getLogger(__name__)

OK = "ok"
FAILED = "failed"
RUNNING = "running"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load() -> dict:
    p = config.path(config.STATE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        # State is a record of what happened, not a source of truth about what should happen.
        # A corrupt file must not stop the scheduler; it costs history, not function.
        logger.warning("state file unreadable, starting fresh: %s", e)
        return {}


def save(data: dict) -> None:
    config.write_private(config.STATE, json.dumps(data, indent=2, sort_keys=True))


def jobs() -> dict:
    return load().get("jobs", {})


def job(name: str) -> dict:
    return jobs().get(name, {})


def _update_job(name: str, **fields: Any) -> dict:
    data = load()
    data.setdefault("jobs", {}).setdefault(name, {}).update(fields)
    save(data)
    return data["jobs"][name]


def mark_started(name: str) -> None:
    _update_job(name, status=RUNNING, started_at=_now(), last_error="")


def mark_finished(name: str, *, ok: bool, summary: str = "", error: str = "",
                  report: str = "") -> None:
    """Record the outcome, including the full report behind the summary.

    Only the latest report is kept *here*, deliberately: this file is what the dashboard reads
    on every poll, and growing it without bound on a machine that runs unattended for months
    would make the cheapest read in the product the most expensive one. The history lives in
    `reports.py` as one file per run, where reading it is a page an operator asked for rather
    than something every request pays for.
    """
    _update_job(
        name,
        status=OK if ok else FAILED,
        last_run=_now(),
        last_summary=summary,
        last_error=error,
        last_report=report,
    )


def mark_interrupted(name: str) -> datetime | None:
    """Close out a run that was still marked `running` when this process started.

    `mark_started` writes before the work begins and `mark_finished` after it ends, so a process
    killed in between leaves `running` in the file for good — nothing else ever clears it. The
    dashboard then describes a job that stopped days ago as in progress, which is precisely the
    "reports itself as running while nothing is happening" failure this module was written to
    make visible. Producing it would be a poor joke.

    Returns when that run started, or None if the job was not marked running.

    **`last_run` is set to the start of the interrupted run, not to now.** The run did happen —
    dating it now would reset the staleness the dashboard exists to show, hiding a scheduler
    that has been dead for a week behind a fresh-looking timestamp.
    """
    record = job(name)
    if record.get("status") != RUNNING:
        return None

    started = record.get("started_at")
    try:
        when = datetime.fromisoformat(started) if started else None
    except ValueError:
        when = None

    fields: dict[str, Any] = {
        "status": FAILED,
        "last_error": (
            f"Interrupted: Timar stopped during a run that began at "
            f"{started or 'an unknown time'}. An update run that stops here has already woken "
            f"machines and not shut them down again."
        ),
        "last_summary": "",
        "last_report": "",
    }
    if when:
        fields["last_run"] = when.isoformat(timespec="seconds")
    _update_job(name, **fields)
    return when


def set_next_run(name: str, when: datetime | None) -> None:
    _update_job(name, next_run=when.isoformat(timespec="seconds") if when else None)


def beat(name: str) -> None:
    """Record that a supervised task is alive.

    A task that died leaves its heartbeat frozen while the process keeps serving pages
    perfectly — the exact shape of the failure this module exists to surface.
    """
    data = load()
    data.setdefault("heartbeat", {})[name] = _now()
    save(data)


def heartbeats() -> dict:
    return load().get("heartbeat", {})


def last_run(name: str) -> datetime | None:
    raw = job(name).get("last_run")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
