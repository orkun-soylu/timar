"""Schedule arithmetic, and the supervision that is the actual feature."""
from datetime import datetime, timedelta

import pytest

from timar.schedule import DAILY, INTERVAL, WEEKLY, Schedule, ScheduleError, next_run

MONDAY_NOON = datetime(2026, 8, 3, 12, 0)  # a Monday


class TestNextRun:
    def test_disabled_never_runs(self):
        assert next_run(Schedule(enabled=False, kind=DAILY, at="09:00"), MONDAY_NOON) is None

    def test_daily_later_today(self):
        spec = Schedule(enabled=True, kind=DAILY, at="18:00")
        assert next_run(spec, MONDAY_NOON) == datetime(2026, 8, 3, 18, 0)

    def test_daily_already_past_rolls_to_tomorrow(self):
        spec = Schedule(enabled=True, kind=DAILY, at="09:00")
        assert next_run(spec, MONDAY_NOON) == datetime(2026, 8, 4, 9, 0)

    def test_weekly_later_this_week(self):
        spec = Schedule(enabled=True, kind=WEEKLY, day="friday", at="07:00")
        assert next_run(spec, MONDAY_NOON) == datetime(2026, 8, 7, 7, 0)

    def test_weekly_on_the_same_day_but_past_rolls_a_week(self):
        spec = Schedule(enabled=True, kind=WEEKLY, day="monday", at="09:00")
        assert next_run(spec, MONDAY_NOON) == datetime(2026, 8, 10, 9, 0)

    def test_weekly_on_the_same_day_still_ahead_stays_today(self):
        spec = Schedule(enabled=True, kind=WEEKLY, day="monday", at="18:00")
        assert next_run(spec, MONDAY_NOON) == datetime(2026, 8, 3, 18, 0)

    def test_interval_counts_from_the_last_run(self):
        """Otherwise a restart resets the clock, and a six-hourly job restarted hourly never
        fires at all -- silently, which is this project's defining failure mode."""
        spec = Schedule(enabled=True, kind=INTERVAL, every_hours=6)
        last = MONDAY_NOON - timedelta(hours=1)
        assert next_run(spec, MONDAY_NOON, last_run=last) == last + timedelta(hours=6)

    def test_interval_overdue_runs_immediately(self):
        # A run missed while the process was down happens at the next opportunity rather than
        # being quietly counted as done.
        spec = Schedule(enabled=True, kind=INTERVAL, every_hours=6)
        last = MONDAY_NOON - timedelta(hours=20)
        assert next_run(spec, MONDAY_NOON, last_run=last) == MONDAY_NOON

    def test_interval_with_no_history_starts_from_now(self):
        spec = Schedule(enabled=True, kind=INTERVAL, every_hours=4)
        assert next_run(spec, MONDAY_NOON) == MONDAY_NOON + timedelta(hours=4)

    @pytest.mark.parametrize("at", ["25:00", "09:70", "nine", "0900", ""])
    def test_malformed_time_is_an_error_not_a_silent_skip(self, at):
        with pytest.raises(ScheduleError):
            next_run(Schedule(enabled=True, kind=DAILY, at=at), MONDAY_NOON)

    def test_unknown_day_is_an_error(self):
        with pytest.raises(ScheduleError):
            next_run(Schedule(enabled=True, kind=WEEKLY, day="caturday"), MONDAY_NOON)

    def test_describe_reads_as_a_sentence(self):
        assert Schedule(enabled=False).describe() == "not scheduled"
        assert Schedule(enabled=True, kind=DAILY, at="09:00").describe() == "daily at 09:00"
        assert Schedule(enabled=True, kind=WEEKLY, day="friday", at="07:00").describe() \
            == "every Friday at 07:00"
        assert Schedule(enabled=True, kind=INTERVAL, every_hours=6).describe() == "every 6 hours"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    from timar import config, state
    importlib.reload(config)
    importlib.reload(state)
    return tmp_path


class TestSupervision:
    """The reason this module exists.

    An asyncio task that raises does not stop the process — it disappears, while the server
    keeps serving pages and the container stays healthy. These tests pin the two defences.
    """

    @pytest.mark.asyncio
    async def test_a_crashing_loop_is_restarted(self, data_dir, monkeypatch):
        import asyncio

        from timar import scheduler as scheduler_module

        monkeypatch.setattr(scheduler_module, "RESTART_DELAY", 0.01)
        attempts = []

        async def always_crashes():
            attempts.append(1)
            raise RuntimeError("boom")

        sched = scheduler_module.Scheduler()
        task = asyncio.create_task(sched._supervise("test", always_crashes))
        await asyncio.sleep(0.06)
        task.cancel()

        # Without the supervisor this would be exactly 1 and the loop would be gone for good.
        assert len(attempts) > 1

    @pytest.mark.asyncio
    async def test_cancellation_is_shutdown_not_a_crash(self, data_dir):
        import asyncio

        from timar import scheduler as scheduler_module

        restarts = []

        async def sleeps():
            restarts.append(1)
            await asyncio.sleep(3600)

        sched = scheduler_module.Scheduler()
        task = asyncio.create_task(sched._supervise("test", sleeps))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(restarts) == 1  # shutdown must not be treated as a failure to retry

    @pytest.mark.asyncio
    async def test_a_failing_job_is_recorded_not_raised(self, data_dir, monkeypatch):
        """A job that throws must not take the loop that called it down with it."""
        from timar import jobs, scheduler as scheduler_module, state

        def explode(cfg):
            raise ValueError("ssh went wrong")

        monkeypatch.setitem(jobs.RUNNERS, jobs.LOG_SWEEP, explode)
        sched = scheduler_module.Scheduler()
        assert await sched.run(jobs.LOG_SWEEP) is True  # it ran; it did not raise

        record = state.job(jobs.LOG_SWEEP)
        assert record["status"] == state.FAILED
        assert "ssh went wrong" in record["last_error"]
        assert record["last_run"]  # the attempt is still stamped

    @pytest.mark.asyncio
    async def test_a_job_does_not_run_twice_at_once(self, data_dir, monkeypatch):
        """A manual run landing on top of a scheduled one would have two updaters on one host."""
        import asyncio

        from timar import jobs, scheduler as scheduler_module

        started = asyncio.Event()

        def slow(cfg):
            started.set()
            import time
            time.sleep(0.2)
            return "done"

        monkeypatch.setitem(jobs.RUNNERS, jobs.LOG_SWEEP, slow)
        sched = scheduler_module.Scheduler()

        first = asyncio.create_task(sched.run(jobs.LOG_SWEEP))
        await started.wait()
        assert await sched.run(jobs.LOG_SWEEP) is False  # refused while the first is in flight
        assert await first is True

    @pytest.mark.asyncio
    async def test_the_loop_writes_a_heartbeat(self, data_dir):
        """A frozen heartbeat on a healthy-looking container is the only outward sign that a
        loop has stopped."""
        import asyncio

        from timar import jobs, scheduler as scheduler_module, state

        sched = scheduler_module.Scheduler()
        task = asyncio.create_task(sched._job_loop(jobs.LOG_SWEEP))
        await asyncio.sleep(0.05)
        task.cancel()

        assert state.heartbeats().get(f"job:{jobs.LOG_SWEEP}")

    @pytest.mark.asyncio
    async def test_an_invalid_schedule_does_not_kill_the_loop(self, data_dir):
        """An operator typo is an operator error; the loop has to stay up so the UI can fix it."""
        import asyncio

        from timar import config, jobs, scheduler as scheduler_module, state

        config.save({"schedules": {jobs.LOG_SWEEP: {"enabled": True, "kind": "daily", "at": "25:00"}}})
        sched = scheduler_module.Scheduler()
        task = asyncio.create_task(sched._job_loop(jobs.LOG_SWEEP))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        assert state.job(jobs.LOG_SWEEP).get("next_run") is None
