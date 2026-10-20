"""Machines updated in a window of their own, not by the fleet's run.

A gaming PC, a workstation: a machine whose owner says "only between 03:00 and 07:00", which
is not when the fleet is updated and need not be the same day. `update_window: "03:00-07:00"` on
a server takes it out of the fleet run — it is listed there as skipped, with the date of its
last update — and gives it a run of its own, once every `update_every_days` (default 7), on the
first night its window is open.

**One attempt per window, retried the next night.** A run that does not end in an update — the
machine was in use, did not wake, an update failed — leaves the machine due, so the next night's
window tries again. Retrying within the same window instead would wake a machine whose owner has
just sat down at it every few minutes until the window closed. A window whose start has passed
still counts as open: a scheduler that was down at 03:00 runs at 03:40, not a week later.

The record lives in `state.json` under `update_windows`, by server name: the opening of the
window last tried, and when the machine was last updated.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta

from . import config, membership, state

logger = logging.getLogger(__name__)

DEFAULT_EVERY_DAYS = 7
# An update that started at 03:40 last week is due again at 03:05 tonight, not at 03:40: a day
# counted as 24 hours would push the run a little later every week until it left the window.
SLACK = timedelta(hours=12)

_SPEC = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class WindowError(ValueError):
    pass


def parse(spec: str) -> tuple[time, time]:
    """`"03:00-07:00"` → (03:00, 07:00). A window may cross midnight: `"23:00-05:00"`."""
    match = _SPEC.match(str(spec or ""))
    if not match:
        raise WindowError(f"update_window must look like 03:00-07:00, got {spec!r}")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60) or (h1, m1) == (h2, m2):
        raise WindowError(f"update_window {spec!r} is not a range of the day")
    return time(h1, m1), time(h2, m2)


def opening(spec: str, now: datetime) -> datetime | None:
    """When the window that is open at `now` opened, or None when it is closed."""
    start, end = parse(spec)
    today = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    clock = now.time()
    if start < end:
        return today if start <= clock < end else None
    # Crosses midnight: open from `start` to the end of the day, and from midnight to `end`.
    if clock >= start:
        return today
    if clock < end:
        return today - timedelta(days=1)
    return None


def describe(server: dict) -> str:
    """The fleet run's reason for leaving the machine out."""
    spec = server["update_window"]
    last = state.update_windows().get(server["name"], {}).get("updated")
    when = f", last updated {last[:10]}" if last else ", not updated yet"
    return f"updated in its own window, {spec.strip()}{when}"


def due(cfg: dict, now: datetime) -> list[str]:
    """Servers whose window is open now and that have neither been tried in it nor updated
    recently enough."""
    servers = cfg.get("servers", [])
    guests = {vm["server_name"] for s in servers for vm in s.get("manages_vms", [])}
    record = state.update_windows()
    names = []
    for server in servers:
        spec = server.get("update_window")
        if not spec:
            continue
        name = server["name"]
        if name in guests:
            # A guest is updated by its hypervisor's run; a window of its own would need that
            # host woken for it alone. Not supported rather than half-done.
            continue
        # Left out, not enrolled, watched only: a nightly report saying so would be noise.
        if membership.skip_reason(cfg, "update", server):
            continue
        try:
            opened = opening(spec, now)
        except WindowError as e:
            logger.error("%s: %s", name, e)
            continue
        if opened is None:
            continue
        seen = record.get(name, {})
        if seen.get("tried") == opened.isoformat():
            continue
        if last := seen.get("updated"):
            every = timedelta(days=int(server.get("update_every_days") or DEFAULT_EVERY_DAYS))
            if now - datetime.fromisoformat(last) < every - SLACK:
                continue
        names.append(name)
    return names


def mark_tried(name: str, spec: str, now: datetime) -> None:
    if opened := opening(spec, now):
        state.set_update_window(name, tried=opened.isoformat())


def record(cfg: dict, results) -> None:
    """Remember the machines with a window that a run did update."""
    windowed = {s["name"] for s in cfg.get("servers", []) if s.get("update_window")}
    for r in results:
        if r.server in windowed and r.success and not r.skipped:
            state.set_update_window(r.server, updated=datetime.now().isoformat(timespec="seconds"))


def has_window(server: dict) -> bool:
    return bool(server.get("update_window")) and config.has_ssh(server)
