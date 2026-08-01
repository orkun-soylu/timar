"""Is each host up? Asked constantly by the dashboard, so asked in parallel and cached.

A single probe is a TCP connect with a timeout, and the timeout is the normal case here — most
of the fleet is *supposed* to be asleep. Run serially, a fleet of eight machines with six of
them off costs `6 x timeout` before the page renders. Run in parallel, it costs one timeout.

The cache exists for a second reason: the dashboard polls, and without it every poll would
re-probe every host. A few seconds of staleness is invisible to someone watching a status page
and it keeps a browser tab from generating continuous traffic to every machine in the rack.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config
from .network import is_host_up

TTL = 10.0
MAX_PARALLEL = 16
PROBE_TIMEOUT = 3.0

_cache: dict[str, tuple[float, bool]] = {}


@dataclass(frozen=True)
class HostStatus:
    name: str
    host: str
    up: bool
    on_demand: bool
    platform: str

    @property
    def state(self) -> str:
        """What to show the operator — three states, not two.

        A machine that is asleep and is *meant* to be asleep is not in the same condition as one
        that should be up and is not, and a status page that paints both red teaches its reader
        to ignore red.
        """
        if self.up:
            return "up"
        return "asleep" if self.on_demand else "down"


def _probe(host: str) -> bool:
    now = time.monotonic()
    cached = _cache.get(host)
    if cached and now - cached[0] < TTL:
        return cached[1]
    up = is_host_up(host, timeout=PROBE_TIMEOUT)
    _cache[host] = (now, up)
    return up


def invalidate() -> None:
    """Drop the cache — after a wake or a shutdown, where staleness is actively misleading."""
    _cache.clear()


def fleet(cfg: dict) -> list[HostStatus]:
    servers = cfg.get("servers", [])
    if not servers:
        return []

    sleepers = config.on_demand(servers)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(servers))) as pool:
        results = list(pool.map(lambda s: _probe(s["host"]), servers))

    return [
        HostStatus(
            name=s["name"],
            host=s["host"],
            up=up,
            on_demand=s["name"] in sleepers,
            platform=s.get("platform", "linux"),
        )
        for s, up in zip(servers, results)
    ]
