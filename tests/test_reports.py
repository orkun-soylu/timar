"""The report archive.

Each test gets its own empty `/data` via `TIMAR_DATA`, because the archive is a directory on
disk and nothing else.
"""
import importlib
import json
from datetime import datetime

import pytest


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    from timar import config, reports as reports_module
    importlib.reload(config)
    importlib.reload(reports_module)
    return reports_module


def write(reports, job, when, **fields):
    """Archive one entry at a chosen moment, so a series can be built without waiting."""
    import timar.reports
    original = timar.reports.datetime

    class Frozen(original):
        @classmethod
        def now(cls, tz=None):
            return when

    timar.reports.datetime = Frozen
    try:
        return reports.archive(job, title=fields.pop("title", job), ok=fields.pop("ok", True),
                               **fields)
    finally:
        timar.reports.datetime = original


class TestArchiving:
    def test_a_finished_run_is_stored_in_full(self, reports):
        report_id = reports.archive("log_sweep", title="Log sweep", ok=True,
                                    summary="1 with findings", report="pi: disk 91%")
        entry = reports.get(report_id)
        assert entry["job"] == "log_sweep"
        assert entry["summary"] == "1 with findings"
        assert entry["report"] == "pi: disk 91%"
        assert entry["ok"] is True

    def test_a_failed_run_is_archived_too(self, reports):
        """The failing Friday update is the entry an operator most wants three weeks later."""
        report_id = reports.archive("update", title="Update run", ok=False,
                                    error="SSHException: timed out")
        entry = reports.get(report_id)
        assert entry["ok"] is False and "timed out" in entry["error"]

    def test_the_file_is_private(self, reports):
        reports.archive("log_sweep", title="Log sweep", ok=True, report="hostnames and log lines")
        path = next((reports.config.path(reports.DIR)).iterdir())
        assert path.stat().st_mode & 0o077 == 0

    def test_two_runs_in_the_same_second_stay_apart_and_stay_in_order(self, reports):
        """A job that fails immediately can finish twice inside one second.

        Sub-second precision is what keeps them from overwriting each other, and — because the
        filename is the sort key — what keeps the later one listed first.
        """
        first = write(reports, "update", datetime(2026, 8, 3, 9, 15, 0, 100), report="first")
        second = write(reports, "update", datetime(2026, 8, 3, 9, 15, 0, 200), report="second")
        assert first != second
        assert [e["id"] for e in reports.listing()] == [second, first]
        assert reports.get(first)["report"] == "first"

    def test_an_unwritable_archive_does_not_raise(self, reports, monkeypatch):
        """The run happened and its outcome is already recorded; losing the copy is not a failure."""
        monkeypatch.setattr(reports.config, "write_private",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
        assert reports.archive("update", title="Update run", ok=True) is None


class TestListing:
    def test_newest_first(self, reports):
        for day in (1, 3, 2):
            write(reports, "log_sweep", datetime(2026, 8, day, 9, 0, 0), summary=f"day {day}")
        assert [e["summary"] for e in reports.listing()] == ["day 3", "day 2", "day 1"]

    def test_filtering_by_job(self, reports):
        write(reports, "log_sweep", datetime(2026, 8, 1, 9, 0, 0))
        write(reports, "update", datetime(2026, 8, 2, 7, 0, 0))
        assert [e["job"] for e in reports.listing("update")] == ["update"]
        assert len(reports.listing()) == 2

    def test_an_unknown_job_lists_nothing_rather_than_failing(self, reports):
        write(reports, "log_sweep", datetime(2026, 8, 1, 9, 0, 0))
        assert reports.listing("retired_job") == []

    def test_the_listing_leaves_out_the_bodies(self, reports):
        """Several hundred runs would otherwise carry every finding into a page showing none."""
        reports.archive("log_sweep", title="Log sweep", ok=True, report="x" * 5000)
        assert "report" not in reports.listing()[0]

    def test_an_empty_archive_lists_nothing(self, reports):
        assert reports.listing() == [] and reports.counts() == {}

    def test_a_torn_entry_costs_only_itself(self, reports):
        good = write(reports, "update", datetime(2026, 8, 2, 7, 0, 0), summary="fine")
        (reports.config.path(reports.DIR) / "20260801-070000.000000-update.json").write_text("{not json")
        assert [e["id"] for e in reports.listing()] == [good]

    def test_counts_per_job(self, reports):
        write(reports, "log_sweep", datetime(2026, 8, 3, 9, 15, 0, 1))
        write(reports, "log_sweep", datetime(2026, 8, 3, 9, 15, 0, 2))
        write(reports, "update", datetime(2026, 8, 3, 7, 0, 0))
        assert reports.counts() == {"log_sweep": 2, "update": 1}


class TestRetention:
    def test_pruning_is_per_job(self, reports, monkeypatch):
        """A daily sweep must not evict a weekly update run — the rarer report is worth more."""
        monkeypatch.setattr(reports, "KEEP_PER_JOB", 3)
        write(reports, "update", datetime(2026, 7, 1, 7, 0, 0), summary="the only update")
        for day in range(1, 6):
            write(reports, "log_sweep", datetime(2026, 8, day, 9, 0, 0), summary=f"sweep {day}")

        assert reports.counts() == {"log_sweep": 3, "update": 1}
        assert [e["summary"] for e in reports.listing("log_sweep")] == ["sweep 5", "sweep 4", "sweep 3"]

    def test_the_newest_survives_pruning(self, reports, monkeypatch):
        monkeypatch.setattr(reports, "KEEP_PER_JOB", 1)
        write(reports, "update", datetime(2026, 8, 1, 7, 0, 0), summary="old")
        newest = write(reports, "update", datetime(2026, 8, 8, 7, 0, 0), summary="new")
        assert [e["id"] for e in reports.listing()] == [newest]


class TestLookup:
    @pytest.mark.parametrize("report_id", [
        "../auth.json",          # the password hash lives one directory up
        "../../etc/passwd",
        "20260803-091500-update/../../secret_key",
        "not-an-id",
        "",
    ])
    def test_an_id_from_a_url_cannot_reach_outside_the_archive(self, reports, report_id):
        assert reports.get(report_id) is None

    def test_a_missing_entry_is_none_rather_than_an_error(self, reports):
        assert reports.get("20260101-000000.000000-update") is None

    def test_the_id_round_trips(self, reports):
        report_id = reports.archive("update", title="Update run", ok=True, summary="3 updated")
        assert reports.get(report_id)["id"] == report_id
        stored = json.loads((reports.config.path(reports.DIR) / f"{report_id}.json").read_text())
        assert stored["summary"] == "3 updated"
