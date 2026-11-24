"""Services: something that runs on a server but is neither a machine nor a compose project.

An agent in a terminal, a model server, a program started by hand — timar cannot start, stop or
update these, and has no SSH session to read them with (their host may be `ssh: false`). What it
can do is show them where they live, under their host, with a light and a link:

    services:
    - name: opencode@rocm             # letters, digits, . _ - and @
      server: opencode-prod-01        # the row it sits under
      logo: opencode                  # a slug from containers.APP_LOGOS, optional
      web_url: opencode-rocm://open   # http(s) or a `link_schemes` entry, optional
      health_url: https://rocm.lan    # optional: no answer → the service is down
      on_demand: true                 # optional: no answer → asleep (grey), not down
      folded: true                    # optional: hidden under its server until opened

Written in `config.yaml` by hand, like `link_schemes`. Everything in an entry is checked again
where it is drawn, so a mistyped value shows as such instead of reaching the page.

The light: host not up → unknown; host up and no `health_url` → up (the host is all there is to
ask); otherwise the health check decides, and a service marked `on_demand` (its backend is woken
when needed) reads as asleep rather than down while the check does not answer. A health check answers with any status below 500, as
for containers — a login page in front of the service still says it is there.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import containers, status as fleet_status, validate

# A server name's alphabet plus `@`, which reads naturally for "this program on that thing".
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
SYSTEM = "Service"
DEFAULT_LOGO = "linux"
MAX_PARALLEL = 16


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    server: str
    state: str                  # "up", "asleep", "down" or "unknown"
    detail: str
    web_url: str | None = None
    logo: str = DEFAULT_LOGO
    address: str = ""           # the health check's host, if there is one
    folded: bool = False        # hidden under its server until opened; see `web.app._fold`

    @property
    def logo_name(self) -> str:
        return containers.APP_LOGOS.get(self.logo, "Linux")

    @property
    def running(self) -> bool:
        return self.state == "up"


def entries(cfg: dict) -> list[dict]:
    """The well-formed entries: a name in the alphabet above, unique, and a server named.

    A malformed entry is left out rather than failing the page — the rest of the fleet must
    still draw while someone fixes a typo in the file.
    """
    seen: set[str] = set()
    out = []
    for e in cfg.get("services") or []:
        if not isinstance(e, dict):
            continue
        name, server = str(e.get("name") or ""), str(e.get("server") or "")
        if not NAME.match(name) or not server or name in seen:
            continue
        seen.add(name)
        out.append(e)
    return out


def _health_url(e: dict) -> str | None:
    raw = e.get("health_url")
    return validate.web_url(str(raw)) if raw else None


def refresh(cfg: dict) -> None:
    """Ask every health address of a service whose host is up, now — the background half."""
    servers = {s["name"]: s for s in cfg.get("servers", [])}
    urls = list(dict.fromkeys(
        u for e in entries(cfg)
        if (u := _health_url(e)) and e["server"] in servers
        and fleet_status._probe(fleet_status._probe_key(servers[e["server"]]))))
    if urls:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(urls))) as pool:
            list(pool.map(lambda u: containers._healthy(u, fresh=True), urls))


def services(cfg: dict) -> list[ServiceStatus]:
    servers = {s["name"]: s for s in cfg.get("servers", [])}
    schemes = validate.link_schemes(cfg)
    out = []
    for e in entries(cfg):
        server = servers.get(e["server"])
        health = _health_url(e)
        if server is None:
            state, detail = "down", "host not configured"
        elif not fleet_status._probe(fleet_status._probe_key(server)):
            state, detail = "unknown", "host is not up"
        elif health and not containers._healthy(health):
            state = "asleep" if e.get("on_demand") is True else "down"
            detail = "the health check does not answer"
        else:
            state, detail = "up", "running"
        logo = str(e.get("logo") or "")
        raw_link = e.get("web_url")
        out.append(ServiceStatus(
            name=e["name"], server=e["server"], state=state, detail=detail,
            web_url=validate.web_url(str(raw_link), schemes) if raw_link else None,
            logo=logo if logo in containers.APP_LOGOS else DEFAULT_LOGO,
            address=(urlsplit(health).hostname or "") if health else "",
            folded=e.get("folded") is True,
        ))
    return sorted(out, key=lambda s: s.name.casefold())


def sort_services(items: list[ServiceStatus], key: str = "name",
                  descending: bool = False) -> list[ServiceStatus]:
    by_name = sorted(items, key=lambda s: s.name.casefold())
    if key == "host":
        by_name = sorted(by_name, key=lambda s: s.server.casefold())
    return by_name[::-1] if descending else by_name
