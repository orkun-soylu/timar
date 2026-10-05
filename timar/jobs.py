"""The two scheduled jobs, and what they report.

Both are **blocking**: paramiko is synchronous, an update can take ten minutes, and a sweep
walks every host over SSH. The scheduler runs them in a thread, never on the event loop — a job
executed inline would freeze the web UI for the duration, and the dashboard going dead while an
update runs is the opposite of what an operator needs at that moment.

Each job produces two things from one set of results: a one-line `summary` for the job table,
and the full `report`. Both are persisted. The report is built as **plain text** and marked up
for Telegram separately, because it has to render in two places that escape differently, and
storing the Telegram version would put `<pre>` tags on the page.

The two are also built in different languages. The stored copy is English, like everything
timar keeps (`i18n`); the Telegram message is in `telegram.language`, because it is read once,
on a phone, by the operator who chose it.
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from . import analysis, cancel, config, i18n, membership, llm as llm_module, notify, state, status as fleet_status
from .i18n import gettext as _
from .log_checker import run_log_checks
from .updater import UpdateResult, run_updates, wait_for_self_update

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


def _language(cfg: dict) -> str:
    return (cfg.get("telegram") or {}).get("language") or i18n.DEFAULT


def _sweep_body(results, offline, clean: bool) -> str:
    if clean:
        return _("All clear — {checked} checked, {asleep} asleep.",
                 checked=len(results) - len(offline), asleep=len(offline))
    return analysis.format_findings(results)


def run_log_sweep(cfg: dict) -> Outcome:
    results = run_log_checks(cfg)

    offline = [r.server for r in results if r.offline]
    unreachable = [r.server for r in results if not r.success]
    with_issues = [r for r in results if r.success and not r.offline and r.has_issues]

    summary = f"{len(with_issues)} with findings, {len(unreachable)} unreachable, {len(offline)} asleep"
    # Devices that are only watched (`ssh: false`) were never in the sweep; they are not counted.
    left_out = [s["name"] for s in cfg.get("servers", [])
                if config.has_ssh(s) and membership.skip_reason(cfg, LOG_SWEEP, s)]
    if left_out:
        summary += f", {len(left_out)} not in the sweep"
    stopped = cancel.requested(LOG_SWEEP)
    if stopped:
        summary += f" — stopped by the operator after {len(results)} hosts"

    language = _language(cfg)
    # One assessment, written in Telegram's language — it is read there first, and a second
    # model call only to have an English copy in the archive is not worth its cost.
    written = analysis.analyze(
        llm_module.LLMConfig.from_dict(cfg.get("llm")), cfg, results, config.load_notes(),
        language=language,
    )
    clean = not with_issues and not unreachable
    with i18n.using(i18n.DEFAULT):
        body = _sweep_body(results, offline, clean)
    with i18n.using(language):
        lines = [f"<b>{_(TITLES[LOG_SWEEP])}</b>", ""]
        if written:
            lines += [f"<i>{notify.escape(written)}</i>", ""]
        message = _sweep_body(results, offline, clean)
    # The findings block is preformatted so the column alignment survives; the all-clear line is
    # one sentence and reads better without it.
    lines.append(message if clean else f"<pre>{notify.escape(message)}</pre>")

    _notify(cfg, "\n".join(lines))
    return Outcome(summary, f"{written}\n\n{body}" if written else body)


def _handoff(results: list[UpdateResult], host: str, unit: str, deadline: float) -> None:
    state.set_handoff(UPDATE, {"results": [r.to_dict() for r in results],
                               "host": host, "unit": unit, "deadline": deadline})


def run_update(cfg: dict) -> Outcome:
    results = run_updates(cfg, on_handoff=_handoff)
    # A wake or a shutdown makes every cached probe wrong, and a dashboard that still shows a
    # machine asleep ten minutes after Timar woke it is worse than one that shows nothing.
    fleet_status.invalidate()
    return _update_outcome(cfg, results)


def finish_handed_off_update(cfg: dict, handoff: dict) -> Outcome:
    """Finish an update run that this process did not start: the previous one handed the update
    of timar's own host to that host's systemd and was then restarted by it, as intended."""
    results = [UpdateResult(**r) for r in handoff.get("results", [])]
    host = handoff.get("host", "")
    server = next((s for s in cfg.get("servers", []) if s["name"] == host), None)
    if server is None:
        results.append(UpdateResult(server=host or "timar's own host", success=False,
                                    error="handed off, then removed from the config before it reported"))
    else:
        results.append(wait_for_self_update(server, handoff["unit"], float(handoff["deadline"]),
                                            note="timar restarted during it"))
    fleet_status.invalidate()
    return _update_outcome(cfg, results)


def _update_outcome(cfg: dict, results: list[UpdateResult]) -> Outcome:
    failed = [r for r in results if not r.success]
    skipped = [r for r in results if r.skipped]
    updated = [r for r in results if r.success and not r.skipped]
    summary = f"{len(updated)} updated, {len(failed)} failed, {len(skipped)} skipped"
    stopped = cancel.requested(UPDATE)
    if stopped:
        summary += " — stopped by the operator"

    with i18n.using(i18n.DEFAULT):
        rows = _update_rows(results, stopped)
    with i18n.using(_language(cfg)):
        lines = [f"<b>{_(TITLES[UPDATE])}</b>", ""] + [notify.escape(row)
                                                        for row in _update_rows(results, stopped)]
    _notify(cfg, "\n".join(lines))
    return Outcome(summary, "\n".join(rows))


def _update_rows(results: list[UpdateResult], stopped: bool) -> list[str]:
    rows = []
    for r in results:
        if r.skipped:
            rows.append("— " + _("{server}: skipped ({reason})", server=r.server, reason=r.error))
        elif r.success:
            # Whether the machine was woken for this is the detail an operator checks first when
            # a machine they expected to find asleep is running.
            note = (f" ({r.note})" if r.note
                    else "" if r.was_running else f" ({_('woken, updated, shut down again')})")
            rows.append(f"✅ {r.server}{note}")
        else:
            # Not truncated here. The failure text is already bounded where it is produced, and
            # the 300 characters this used to keep were spent on whichever stream came first —
            # cutting away the one that named the cause.
            rows.append(f"❌ {r.server}: {r.error.strip()}".replace("\n", "\n    "))

    if stopped:
        rows.append("⏹ " + _("Stopped by the operator: the machines after the last one listed were "
                             "not reached and are not updated."))
    return rows


RUNNERS = {LOG_SWEEP: run_log_sweep, UPDATE: run_update}
