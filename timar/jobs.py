"""The two scheduled jobs, and what they report.

Both are **blocking**: paramiko is synchronous, an update can take ten minutes, and a sweep
walks every host over SSH. The scheduler runs them in a thread, never on the event loop — a job
executed inline would freeze the web UI for the duration, and the dashboard going dead while an
update runs is the opposite of what an operator needs at that moment.

Each job produces two things from one set of results: a one-line `summary` for the job table,
and the full `report`. Both are persisted. The report is built as **plain text** and marked up
for Telegram separately, because it has to render in two places that escape differently, and
storing the Telegram version would put `<pre>` tags on the page.
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from . import analysis, config, llm as llm_module, notify, status as fleet_status
from .log_checker import run_log_checks
from .updater import run_updates

logger = logging.getLogger(__name__)


class Outcome(NamedTuple):
    """What a job leaves behind: the line in the table, and the detail behind it."""

    summary: str
    report: str

LOG_SWEEP = "log_sweep"
UPDATE = "update"
JOBS = (LOG_SWEEP, UPDATE)

TITLES = {LOG_SWEEP: "Log sweep", UPDATE: "Update run"}


def _notify(cfg: dict, text: str) -> None:
    """Deliver a report if notifications are configured. Never fatal.

    A delivery failure must not mark the job itself as failed: the sweep ran, the report is
    saved and readable in the UI, and conflating "could not reach Telegram" with "the update
    broke" sends the operator to the wrong problem.

    Returning early when no token is set is why the report has to be persisted rather than only
    sent. An installation with no Telegram configured swept its fleet, formatted the findings,
    and dropped them on the floor — leaving "1 with findings" on the dashboard and no way to
    learn what the finding was.
    """
    telegram = cfg.get("telegram") or {}
    if not telegram.get("token"):
        return
    try:
        notify.send(telegram["token"], telegram["chat_id"], text)
    except notify.NotifyError as e:
        logger.error("could not deliver report: %s", e)


def run_log_sweep(cfg: dict) -> Outcome:
    results = run_log_checks(cfg)

    offline = [r.server for r in results if r.offline]
    unreachable = [r.server for r in results if not r.success]
    with_issues = [r for r in results if r.success and not r.offline and r.has_issues]

    summary = f"{len(with_issues)} with findings, {len(unreachable)} unreachable, {len(offline)} asleep"

    written = analysis.analyze(
        llm_module.LLMConfig.from_dict(cfg.get("llm")), cfg, results, config.load_notes()
    )
    clean = not with_issues and not unreachable
    body = (
        f"All clear — {len(results) - len(offline)} checked, {len(offline)} asleep."
        if clean else analysis.format_findings(results)
    )

    lines = [f"<b>{TITLES[LOG_SWEEP]}</b>", ""]
    if written:
        lines += [f"<i>{notify.escape(written)}</i>", ""]
    # The findings block is preformatted so the column alignment survives; the all-clear line is
    # one sentence and reads better without it.
    lines.append(body if clean else f"<pre>{notify.escape(body)}</pre>")

    _notify(cfg, "\n".join(lines))
    return Outcome(summary, f"{written}\n\n{body}" if written else body)


def run_update(cfg: dict) -> Outcome:
    results = run_updates(cfg)
    # A wake or a shutdown makes every cached probe wrong, and a dashboard that still shows a
    # machine asleep ten minutes after Timar woke it is worse than one that shows nothing.
    fleet_status.invalidate()

    failed = [r for r in results if not r.success]
    skipped = [r for r in results if r.skipped]
    updated = [r for r in results if r.success and not r.skipped]
    summary = f"{len(updated)} updated, {len(failed)} failed, {len(skipped)} skipped"

    rows = []
    for r in results:
        if r.skipped:
            rows.append(f"— {r.server}: skipped ({r.error})")
        elif r.success:
            # Whether the machine was woken for this is the detail an operator checks first when
            # a machine they expected to find asleep is running.
            note = "" if r.was_running else " (woken, updated, shut down again)"
            rows.append(f"✅ {r.server}{note}")
        else:
            # Not truncated here. The failure text is already bounded where it is produced, and
            # the 300 characters this used to keep were spent on whichever stream came first —
            # cutting away the one that named the cause.
            rows.append(f"❌ {r.server}: {r.error.strip()}".replace("\n", "\n    "))

    lines = [f"<b>{TITLES[UPDATE]}</b>", ""] + [notify.escape(row) for row in rows]
    _notify(cfg, "\n".join(lines))
    return Outcome(summary, "\n".join(rows))


RUNNERS = {LOG_SWEEP: run_log_sweep, UPDATE: run_update}
