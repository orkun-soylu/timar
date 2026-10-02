"""Is each host up? Asked constantly by the dashboard, so asked in parallel and cached.

A single probe is a TCP connect with a timeout, and the timeout is the normal case here — most
of the fleet is *supposed* to be asleep. Run serially, a fleet of eight machines with six of
them off costs `6 x timeout` before the page renders. Run in parallel, it costs one timeout.

Nobody waits for a probe, either. A background task (`refresh`, started with the app) asks
every host every `REFRESH_EVERY` seconds and the pages read what it found last — measured, the
servers page went from three seconds (one probe timeout, for the machines that are off) to
nothing. A request probes only when there is no answer yet, or the last one is older than
`STALE_AFTER`: then the background task has stopped, and a stale page would be worse than a
slow one.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, validate
from .network import address_key, is_host_up

REFRESH_EVERY = 10.0
STALE_AFTER = 60.0
MAX_PARALLEL = 16
# A machine that is up answers its SSH port in milliseconds — on the LAN and through a tunnel
# alike — so a second is ample. The timeout is what an *off* machine costs.
PROBE_TIMEOUT = 1.0

_cache: dict[str, tuple[float, bool]] = {}


@dataclass(frozen=True)
class HostStatus:
    name: str
    host: str
    up: bool
    on_demand: bool
    platform: str
    # False when Timar has nothing to start it with — no MAC, no hypervisor. It can still be shut
    # down; it gets no wake button, and its shutdown warns that it will stay off.
    wakeable: bool = True
    # The machine's own web interface. The name becomes a link to it while the machine is up —
    # only then, because a link to a panel that is off is a click that ends in a timeout.
    web_url: str | None = None

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


def _probe(host: str, fresh: bool = False) -> bool:
    now = time.monotonic()
    cached = _cache.get(host)
    if cached and not fresh and now - cached[0] < STALE_AFTER:
        return cached[1]
    up = is_host_up(host, timeout=PROBE_TIMEOUT)
    _cache[host] = (time.monotonic(), up)
    return up


def refresh(cfg: dict) -> None:
    """Ask every host now and keep the answers — the background task's half of the cache."""
    hosts = list(dict.fromkeys(s["host"] for s in cfg.get("servers", [])))
    if hosts:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(hosts))) as pool:
            list(pool.map(lambda h: _probe(h, fresh=True), hosts))


def invalidate() -> None:
    """Drop the cache — after a wake or a shutdown, where staleness is actively misleading."""
    _cache.clear()


def _link(url: str | None, schemes: frozenset[str]) -> str | None:
    """The stored web interface, checked again where it is drawn.

    `config.yaml` is edited by hand as well as through the form, and the form's check is the
    only one a hand-written value would otherwise skip. It is cheap, and it is what keeps a
    `javascript:` typed into the file — or a scheme since removed from `link_schemes` — out of
    the page.
    """
    return validate.web_url(url, schemes) if url else None


def fleet(cfg: dict) -> list[HostStatus]:
    servers = cfg.get("servers", [])
    if not servers:
        return []

    sleepers = config.on_demand(servers)
    schemes = validate.link_schemes(cfg)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(servers))) as pool:
        results = list(pool.map(lambda s: _probe(s["host"]), servers))

    # By name, not config order: the order hosts were enrolled in means nothing to the reader,
    # and a host is found faster in an alphabetical list.
    hosts = [
        HostStatus(
            name=s["name"],
            host=s["host"],
            up=up,
            on_demand=s["name"] in sleepers,
            platform=s.get("platform", "linux"),
            wakeable=config.can_wake(s["name"], servers),
            web_url=_link(s.get("web_url"), schemes),
        )
        for s, up in zip(servers, results)
    ]
    return sorted(hosts, key=lambda h: h.name.casefold())


SORT_KEYS = ("name", "address", "platform")


def sort_fleet(hosts: list[HostStatus], key: str, descending: bool = False) -> list[HostStatus]:
    """Order the dashboard by one column; ties fall back to the name so the order is stable."""
    by_name = sorted(hosts, key=lambda h: h.name.casefold())
    if key == "address":
        ordered = sorted(by_name, key=lambda h: address_key(h.host))
    elif key == "platform":
        ordered = sorted(by_name, key=lambda h: h.platform.casefold())
    else:
        ordered = by_name
    return ordered[::-1] if descending else ordered
