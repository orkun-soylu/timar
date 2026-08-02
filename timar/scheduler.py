"""Runs the scheduled jobs, and — more importantly — proves it is still running.

Everything here lives in the web process's event loop. One process means one PID, one log
stream, and `restart: unless-stopped` meaning what it says. But a single asyncio process has a
failure mode worth stating plainly:

> **A task that raises does not stop the process. It disappears.**

The server keeps serving pages, the container stays `healthy`, and the schedule silently never
fires again. That is almost exactly how Timar's predecessor lost its weekly update job for two
weeks — different mechanism, same shape: something reported itself as running while the work
had stopped.

Two things follow, and they are the actual feature here:

1. **Every long-lived task runs under a supervisor** that catches its exception, logs it, and
   restarts it after a pause. A crash costs one cycle, not the schedule.
2. **Every cycle writes a heartbeat.** A frozen heartbeat on a healthy-looking container is the
   only outward sign that a loop has stopped, so the dashboard shows it.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime

from . import config, jobs, schedule as schedule_module, state

logger = logging.getLogger(__name__)

# How long a supervised loop waits before restarting after an unexpected exception. Long enough
# that a persistent failure does not spin, short enough to recover from a transient one.
RESTART_DELAY = 30.0

# The loop wakes at least this often even when the next run is days away, so the heartbeat stays
# fresh and a schedule edited in the UI is picked up without a restart.
TICK = 60.0


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        # One lock per job: a manual run and a scheduled run must never overlap, and neither
        # must two scheduled runs when one overruns its own interval.
        self._locks = {name: asyncio.Lock() for name in jobs.JOBS}
        self._running: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for name in jobs.JOBS:
            self._tasks.append(asyncio.create_task(
                self._supervise(f"job:{name}", lambda n=name: self._job_loop(n))
            ))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # -- supervision ---------------------------------------------------------

    async def _supervise(self, label: str, factory) -> None:
        """Keep a loop alive across exceptions, and never swallow one silently."""
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise  # shutdown, not a failure
            except Exception:
                # Logged in full: this is the trace that would otherwise vanish with the task.
                logger.error("supervised task %s crashed, restarting in %.0fs:\n%s",
                             label, RESTART_DELAY, traceback.format_exc())
                await asyncio.sleep(RESTART_DELAY)

    # -- the job loop --------------------------------------------------------

    async def _job_loop(self, name: str) -> None:
        while True:
            state.beat(f"job:{name}")
            cfg = config.load()
            spec = schedule_module.Schedule.from_dict((cfg.get("schedules") or {}).get(name))

            try:
                due = schedule_module.next_run(spec, datetime.now(), state.last_run(name))
            except schedule_module.ScheduleError as e:
                # A schedule that cannot be parsed is an operator error, not a crash. Report it
                # against the job and keep the loop alive so the UI can be used to fix it.
                logger.error("%s has an invalid schedule: %s", name, e)
                state.set_next_run(name, None)
                await asyncio.sleep(TICK)
                continue

            state.set_next_run(name, due)

            if due is None:
                await asyncio.sleep(TICK)
                continue

            wait = (due - datetime.now()).total_seconds()
            if wait > 0:
                # Capped at TICK rather than slept through in one go: a long sleep would keep
                # the heartbeat frozen for days and ignore a schedule changed in the meantime.
                await asyncio.sleep(min(wait, TICK))
                continue

            await self.run(name)

    # -- running a job -------------------------------------------------------

    def is_running(self, name: str) -> bool:
        return name in self._running

    async def run(self, name: str) -> bool:
        """Run one job. Returns False if it was already running.

        Non-blocking for the caller in the sense that matters: the work itself happens in a
        worker thread, so the event loop keeps serving the UI throughout.
        """
        lock = self._locks[name]
        if lock.locked():
            logger.info("%s is already running, ignoring the request", name)
            return False

        async with lock:
            self._running.add(name)
            state.mark_started(name)
            logger.info("%s started", name)
            try:
                cfg = config.load()
                outcome = await asyncio.to_thread(jobs.RUNNERS[name], cfg)
                state.mark_finished(
                    name, ok=True, summary=outcome.summary, report=outcome.report
                )
                logger.info("%s finished: %s", name, outcome.summary)
                return True
            except Exception as e:
                # The failure is recorded against the job and shown in the UI. Re-raising here
                # would kill the loop that called us, which is the failure this module exists
                # to prevent.
                logger.exception("%s failed", name)
                state.mark_finished(name, ok=False, error=f"{type(e).__name__}: {e}")
                return True
            finally:
                self._running.discard(name)


scheduler = Scheduler()
