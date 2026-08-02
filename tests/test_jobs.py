"""What a job leaves behind after it runs.

The regression these guard: a sweep on an installation with no Telegram token used to format
its findings, hand them to a notifier that returned early, and discard them. The dashboard said
"1 with findings" and there was no way to learn which finding, on which host.
"""
import pytest

from timar import jobs
from timar.log_checker import LogResult
from timar.updater import UpdateResult


@pytest.fixture
def sweep(monkeypatch):
    """Drive `run_log_sweep` with fixed results and no network of any kind."""
    def run(results, cfg=None, assessment=""):
        monkeypatch.setattr(jobs, "run_log_checks", lambda _cfg: results)
        monkeypatch.setattr(jobs.analysis, "analyze", lambda *a, **k: assessment)
        monkeypatch.setattr(jobs.config, "load_notes", lambda: "")
        return jobs.run_log_sweep(cfg or {})
    return run


@pytest.fixture
def update(monkeypatch):
    def run(results, cfg=None):
        monkeypatch.setattr(jobs, "run_updates", lambda _cfg: results)
        monkeypatch.setattr(jobs.fleet_status, "invalidate", lambda: None)
        return jobs.run_update(cfg or {})
    return run


class TestReportSurvivesWithoutTelegram:
    def test_findings_are_returned_when_no_notifier_is_configured(self, sweep):
        """The whole point: no token must not mean no record."""
        outcome = sweep([
            LogResult(server="web-01", success=True, containers_stopped=["cache", "queue"]),
        ])
        assert outcome.summary == "1 with findings, 0 unreachable, 0 asleep"
        assert "web-01" in outcome.report
        assert "cache" in outcome.report and "queue" in outcome.report

    def test_unreachable_host_names_the_host_and_the_reason(self, sweep):
        outcome = sweep([
            LogResult(server="router", success=False, error="key was not accepted"),
        ])
        assert "router" in outcome.report
        assert "key was not accepted" in outcome.report

    def test_a_clean_sweep_still_says_so(self, sweep):
        outcome = sweep([
            LogResult(server="web-01", success=True),
            LogResult(server="gpu-01", success=True, offline=True),
        ])
        assert outcome.summary == "0 with findings, 0 unreachable, 1 asleep"
        assert "All clear" in outcome.report
        assert "1 checked" in outcome.report and "1 asleep" in outcome.report


class TestReportIsPlainText:
    """It renders on an HTML page, so the stored copy must not carry Telegram's markup."""

    def test_no_markup_leaks_into_the_stored_report(self, sweep):
        outcome = sweep([LogResult(server="web-01", success=True, disk_issues=["/: 91%"])])
        for tag in ("<pre>", "</pre>", "<b>", "<i>"):
            assert tag not in outcome.report

    def test_an_ampersand_in_a_host_name_is_not_escaped_in_the_stored_report(self, sweep):
        """`notify.escape` is for the wire. Escaping here would show `&amp;` on the page."""
        outcome = sweep([LogResult(server="a&b", success=False, error="down")])
        assert "a&b" in outcome.report
        assert "&amp;" not in outcome.report

    def test_the_assessment_leads_the_report_when_a_model_wrote_one(self, sweep):
        outcome = sweep(
            [LogResult(server="web-01", success=True, disk_issues=["/: 91%"])],
            assessment="Disk on web-01 is filling.",
        )
        assert outcome.report.startswith("Disk on web-01 is filling.")
        assert "/: 91%" in outcome.report


class TestUpdateReport:
    def test_each_host_appears_with_its_outcome(self, update):
        outcome = update([
            UpdateResult(server="web-01", success=True),
            UpdateResult(server="gpu-01", success=True, was_running=False),
            UpdateResult(server="router", success=False, skipped=True, error="no update command"),
            UpdateResult(server="db-01", success=False, error="dpkg was interrupted"),
        ])
        # `skipped` counts as a failure too — it did not update, and the summary says so.
        assert outcome.summary == "2 updated, 2 failed, 1 skipped"
        assert "woken, updated, shut down again" in outcome.report
        assert "dpkg was interrupted" in outcome.report
        assert "no update command" in outcome.report

    def test_a_failure_reason_is_not_truncated_away_entirely(self, update):
        outcome = update([UpdateResult(server="db-01", success=False, error="x" * 500)])
        assert "x" * 300 in outcome.report
