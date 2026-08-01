"""The two scheduled jobs, and what they report.

Both are **blocking**: paramiko is synchronous, an update can take ten minutes, and a sweep
walks every host over SSH. The scheduler runs them in a thread, never on the event loop — a job
executed inline would freeze the web UI for the duration, and the dashboard going dead while an
update runs is the opposite of what an operator needs at that moment.
"""
from __future__ import annotations

import logging

from . import analysis, config, llm as llm_module, notify, status as fleet_status
from .log_checker import run_log_checks
from .updater import run_updates

logger = logging.getLogger(__name__)

LOG_SWEEP = "log_sweep"
UPDATE = "update"
JOBS = (LOG_SWEEP, UPDATE)

TITLES = {LOG_SWEEP: "Log sweep", UPDATE: "Update run"}


def _notify(cfg: dict, text: str) -> None:
    """Deliver a report if notifications are configured. Never fatal.

    A delivery failure must not mark the job itself as failed: the sweep ran, the findings are
    in the UI, and conflating "could not reach Telegram" with "the update broke" sends the
    operator to the wrong problem.
    """
    telegram = cfg.get("telegram") or {}
    if not telegram.get("token"):
        return
    try:
        notify.send(telegram["token"], telegram["chat_id"], text)
    except notify.NotifyError as e:
        logger.error("could not deliver report: %s", e)


def run_log_sweep(cfg: dict) -> str:
    results = run_log_checks(cfg)

    offline = [r.server for r in results if r.offline]
    unreachable = [r.server for r in results if not r.success]
    with_issues = [r for r in results if r.success and not r.offline and r.has_issues]

    summary = f"{len(with_issues)} with findings, {len(unreachable)} unreachable, {len(offline)} asleep"

    lines = [f"<b>{TITLES[LOG_SWEEP]}</b>", ""]
    written = analysis.analyze(
        llm_module.LLMConfig.from_dict(cfg.get("llm")), cfg, results, config.load_notes()
    )
    if written:
        lines += [f"<i>{notify.escape(written)}</i>", ""]

    if not with_issues and not unreachable:
        lines.append(f"All clear — {len(results) - len(offline)} checked, {len(offline)} asleep.")
    else:
        lines.append(f"<pre>{notify.escape(analysis.format_findings(results))}</pre>")

    _notify(cfg, "\n".join(lines))
    return summary


def run_update(cfg: dict) -> str:
    results = run_updates(cfg)
    # A wake or a shutdown makes every cached probe wrong, and a dashboard that still shows a
    # machine asleep ten minutes after Timar woke it is worse than one that shows nothing.
    fleet_status.invalidate()

    failed = [r for r in results if not r.success]
    skipped = [r for r in results if r.skipped]
    updated = [r for r in results if r.success and not r.skipped]
    summary = f"{len(updated)} updated, {len(failed)} failed, {len(skipped)} skipped"

    lines = [f"<b>{TITLES[UPDATE]}</b>", ""]
    for r in results:
        if r.skipped:
            lines.append(f"— {notify.escape(r.server)}: skipped ({notify.escape(r.error)})")
        elif r.success:
            # Whether the machine was woken for this is the detail an operator checks first when
            # a machine they expected to find asleep is running.
            note = "" if r.was_running else " (woken, updated, shut down again)"
            lines.append(f"✅ {notify.escape(r.server)}{note}")
        else:
            lines.append(f"❌ {notify.escape(r.server)}: {notify.escape(r.error[:300])}")

    _notify(cfg, "\n".join(lines))
    return summary


RUNNERS = {LOG_SWEEP: run_log_sweep, UPDATE: run_update}
