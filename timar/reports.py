"""The report archive: every run a job has finished, kept on disk.

`state.json` holds only the *latest* report per job, and that is the right thing for the
dashboard — it answers "what did the last sweep find". It cannot answer the question an
operator actually asks a week later: **when did this start?** A disk that crossed 90% last
Tuesday, a host that has been unreachable for three sweeps, an update that has failed every
Friday for a month — none of those are visible in a single snapshot. They are only visible as
a series.

Telegram is a series, which is why the answer used to be "scroll up in the chat". That copy is
outside the tool, it is not searchable by job, it is lost when the chat is cleared, and an
installation with no Telegram configured never had it at all. So the archive is kept here and
the notification becomes what it should have been all along: a *copy*, not the record.

## Shape

One JSON file per run, under `/data/reports/`:

```
20260803-091500-log_sweep.json
20260803-070000-update.json
```

A directory of small files rather than rows appended to `state.json`, for three reasons that
all point the same way. Writing a report cannot rewrite — and so cannot corrupt or lose — the
job state the scheduler depends on. Pruning is `unlink`, not a read-modify-write of a file that
grows without bound on a machine whose whole job is to run unattended for months. And the
filename carries the timestamp and the job, so listing and filtering never open a file.

**Retention is per job, not overall.** A sweep that runs daily and an update that runs weekly
share the archive; one global cap of N would silently evict every update run to make room for
sweeps, which is precisely backwards — the rarer report is the more valuable one.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

DIR = "reports"

# Per job. Roughly a year of daily sweeps; a few kilobytes each. Bounded because the target
# machine may be a Pi with an SD card, and an archive that fills the volume takes the tool it
# belongs to down with it.
KEEP_PER_JOB = 400

# Microseconds, though nothing here is timed that finely. **The name is the sort key** — the
# listing orders by filename and pruning drops from the end of that order, so two entries the
# format cannot separate are two entries that can be returned in the wrong order and pruned in
# the wrong order. Seconds alone are not enough: a job that fails immediately can finish twice
# inside one, and a `-2` disambiguating suffix sorts *before* the entry it followed, which
# silently reverses them.
STAMP = "%Y%m%d-%H%M%S.%f"

ID_PATTERN = re.compile(r"\A\d{8}-\d{6}\.\d{6}-(?P<job>[a-z_]+)\Z")


def _dir() -> Path:
    return config.path(DIR)


def _stem(when: datetime, job: str) -> str:
    """A filename that sorts chronologically and names its job without being opened."""
    return f"{when.strftime(STAMP)}-{job}"


def archive(job: str, *, title: str, ok: bool, summary: str = "", error: str = "",
            report: str = "") -> str | None:
    """Record a finished run. Returns its id, or None if it could not be written.

    Never raises. A job whose work succeeded must not be reported as failed because the
    archive copy could not be saved — the run happened, the outcome is already in `state.json`
    and already sent to Telegram, and losing one entry of history is not worth losing the
    knowledge that the fleet is fine.
    """
    now = datetime.now()
    try:
        stem = _stem(now, job)
        config.write_private(f"{DIR}/{stem}.json", json.dumps({
            "job": job,
            "title": title,
            "finished_at": now.isoformat(timespec="seconds"),
            "ok": ok,
            "summary": summary,
            "error": error,
            "report": report,
        }, indent=2))
    except OSError as e:
        logger.error("could not archive the %s report: %s", job, e)
        return None

    _prune(job)
    return stem


def _prune(job: str) -> None:
    """Drop the oldest entries for one job, leaving the archive bounded.

    Failure here is logged and swallowed for the same reason as in `archive`: an archive that
    is one file too long is not a reason to fail a run.
    """
    try:
        surplus = _files(job)[KEEP_PER_JOB:]
    except OSError as e:
        logger.error("could not read the report archive: %s", e)
        return
    for path in surplus:
        try:
            path.unlink()
        except OSError as e:
            logger.error("could not prune %s: %s", path.name, e)


def _files(job: str | None = None) -> list[Path]:
    """Archived reports, newest first. Filtering by job reads no file, only names."""
    d = _dir()
    if not d.is_dir():
        return []
    suffix = f"-{job}.json" if job else ".json"
    return sorted(
        (p for p in d.iterdir() if p.name.endswith(suffix)),
        key=lambda p: p.name,
        reverse=True,
    )


def _read(path: Path) -> dict | None:
    try:
        entry = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        # One unreadable file must not empty the whole page. The archive is history, and a
        # torn entry costs that entry.
        logger.warning("skipping unreadable report %s: %s", path.name, e)
        return None
    entry["id"] = path.stem
    return entry


def listing(job: str | None = None, limit: int | None = None) -> list[dict]:
    """Archived runs, newest first, without their bodies.

    The report text is dropped here on purpose: a listing of several hundred runs would
    otherwise carry every finding ever recorded into a page that shows none of them.
    """
    entries = []
    for path in _files(job)[:limit]:
        if entry := _read(path):
            entry.pop("report", None)
            entries.append(entry)
    return entries


def get(report_id: str) -> dict | None:
    """One archived run in full, or None if there is no such entry.

    The id comes from a URL, so it is matched against a pattern rather than trusted: an id of
    `../auth.json` would otherwise read the password hash out of the volume and render it.
    """
    if not ID_PATTERN.match(report_id):
        return None
    path = _dir() / f"{report_id}.json"
    return _read(path) if path.is_file() else None


def counts() -> dict[str, int]:
    """How many runs are archived per job — the dropdown says so before it is used."""
    tally: dict[str, int] = {}
    for path in _files():
        if match := ID_PATTERN.match(path.stem):
            job = match.group("job")
            tally[job] = tally.get(job, 0) + 1
    return tally
