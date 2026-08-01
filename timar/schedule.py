"""When does a job run next?

A pure function over a small vocabulary, deliberately not a cron parser. The schedules a
homelab actually needs are "every few hours", "daily at a time" and "weekly on a day" — and a
cron expression is a thing operators mistype in ways that are invisible until the run does not
happen. A dropdown and a time field cannot express `0 7 * * 5` wrongly.

Times are the container's local time. Set `TZ` on the container if that is not what you want; a
report scheduled for 07:00 arriving at 04:00 is the usual symptom of leaving it at UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DAILY = "daily"
WEEKLY = "weekly"
INTERVAL = "interval"
KINDS = (DAILY, WEEKLY, INTERVAL)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    enabled: bool = False
    kind: str = DAILY
    at: str = "09:00"
    day: str = "monday"
    every_hours: int = 6

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Schedule":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            kind=raw.get("kind", DAILY),
            at=str(raw.get("at", "09:00")),
            day=str(raw.get("day", "monday")).lower(),
            every_hours=int(raw.get("every_hours", 6)),
        )

    def to_dict(self) -> dict:
        entry: dict = {"enabled": self.enabled, "kind": self.kind}
        if self.kind == INTERVAL:
            entry["every_hours"] = self.every_hours
        else:
            entry["at"] = self.at
            if self.kind == WEEKLY:
                entry["day"] = self.day
        return entry

    def describe(self) -> str:
        if not self.enabled:
            return "not scheduled"
        if self.kind == INTERVAL:
            hours = self.every_hours
            return f"every {hours} hours" if hours != 1 else "every hour"
        if self.kind == WEEKLY:
            return f"every {self.day.capitalize()} at {self.at}"
        return f"daily at {self.at}"


def _time_of_day(at: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = at.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as e:
        raise ScheduleError(f"time must look like 07:00, got {at!r}") from e
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"time out of range: {at!r}")
    return hour, minute


def next_run(schedule: Schedule, after: datetime, last_run: datetime | None = None) -> datetime | None:
    """The next moment this job should fire, or None when it is not scheduled.

    `last_run` matters only for interval schedules, which are relative: without it, a restart
    would reset the clock and a six-hourly job restarted every hour would never fire at all.
    """
    if not schedule.enabled:
        return None

    if schedule.kind == INTERVAL:
        hours = schedule.every_hours
        if hours < 1:
            raise ScheduleError("interval must be at least one hour")
        base = last_run or after
        candidate = base + timedelta(hours=hours)
        # A job that was due while the process was down runs at the next opportunity rather
        # than being counted as already done.
        return candidate if candidate > after else after

    hour, minute = _time_of_day(schedule.at)
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule.kind == DAILY:
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if schedule.kind == WEEKLY:
        if schedule.day not in DAYS:
            raise ScheduleError(f"unknown day {schedule.day!r}")
        target = DAYS.index(schedule.day)
        ahead = (target - candidate.weekday()) % 7
        candidate += timedelta(days=ahead)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    raise ScheduleError(f"unknown schedule kind {schedule.kind!r}")
