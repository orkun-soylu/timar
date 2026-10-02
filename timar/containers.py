"""Docker Compose projects on the fleet's hosts: what state each is in, and starting and stopping.

The unit is a compose *project* — one directory, one `docker-compose.yml`, however many
containers it starts. That is the unit an operator updates, starts and stops; listing every
container of every project would turn a page of twenty services into a page of sixty rows, most
of them a database nobody touches on its own.

State is read with one `docker ps -a` per host, over the SSH connection Timar already has, and
grouped by the compose working-directory label. Asked in parallel and cached like the host
probe: the page polls, and without the cache every poll would open an SSH session to every host.
A host that is not up is not asked at all — its projects are reported as unknown, not as down.
"""
from __future__ import annotations

import json
import re
import shlex
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from . import status as fleet_status
from .platforms import get as get_platform
from .ssh import connect, run

# Read like the host probe (see `status`): the background task refreshes, pages read the last
# answer, and a request asks for itself only when there is none or it is older than this.
STALE_AFTER = 60.0
MAX_PARALLEL = 8
SSH_TIMEOUT = 15
HEALTH_TIMEOUT = 4.0
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"

# Plain `docker` when the account is in the docker group, `sudo -n docker` when it is not —
# decided on the host, so an operator does not have to tell Timar which of the two applies.
DOCKER = _DOCKER = 'if docker info >/dev/null 2>&1; then D=docker; else D="sudo -n docker"; fi; '

_cache: dict[str, tuple[float, "HostContainers"]] = {}
_health_cache: dict[str, tuple[float, bool]] = {}


class ContainerError(RuntimeError):
    """Something an operator can read: the host is down, docker refused, compose failed."""


@dataclass
class Container:
    id: str
    name: str
    image: str
    state: str          # running, exited, created, restarting, paused, dead
    health: str         # healthy, unhealthy, starting, none
    exit_code: int | None


@dataclass
class HostContainers:
    """One host's answer: containers by compose working directory, or why there is none."""
    by_dir: dict[str, list[Container]] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class ProjectStatus:
    name: str
    server: str
    path: str
    state: str          # up, warn, asleep, down, unknown
    detail: str         # the word for the light's tooltip
    images: tuple[str, ...]
    on_demand: bool
    web_url: str | None
    is_self: bool       # Timar's own project: it is not offered a stop or a restart
    logo: str = "docker"  # the light before the name; see `app_logo`

    @property
    def logo_name(self) -> str:
        return APP_LOGOS.get(self.logo, "Docker")

    @property
    def running(self) -> bool:
        return self.state in ("up", "warn")


# Slug → name, for each application symbol in `web/static/logos.svg` (Simple Icons, CC0).
APP_LOGOS = {
    "docker": "Docker", "forgejo": "Forgejo", "gitea": "Gitea", "gitlab": "GitLab",
    "homepage": "Homepage", "immich": "Immich", "traefikproxy": "Traefik", "vaultwarden": "Vaultwarden",
    "bitwarden": "Bitwarden", "portainer": "Portainer", "searxng": "SearXNG", "ollama": "Ollama",
    "wireguard": "WireGuard", "speedtest": "Speedtest", "grafana": "Grafana", "prometheus": "Prometheus",
    "postgresql": "PostgreSQL", "redis": "Redis", "mariadb": "MariaDB", "mysql": "MySQL",
    "mongodb": "MongoDB", "nextcloud": "Nextcloud", "jellyfin": "Jellyfin", "plex": "Plex",
    "homeassistant": "Home Assistant", "pihole": "Pi-hole", "adguard": "AdGuard", "nginx": "NGINX",
    "caddy": "Caddy", "uptimekuma": "Uptime Kuma", "syncthing": "Syncthing",
    "paperlessngx": "Paperless-ngx", "minio": "MinIO", "influxdb": "InfluxDB", "rabbitmq": "RabbitMQ",
    "n8n": "n8n",
}

# Names that are not their logo's slug: an image is `traefik`, a database `postgres`.
_APP_ALIASES = {
    "traefik": "traefikproxy", "postgres": "postgresql", "pgvecto-rs": "postgresql",
    "home-assistant": "homeassistant", "uptime-kuma": "uptimekuma", "paperless-ngx": "paperlessngx",
    "adguardhome": "adguard", "mongo": "mongodb", "influxdb2": "influxdb",
}

# What a project *uses* rather than what it *is*. A photo library ships its own database, and the
# row should look like the library: these lose to any other match among the same project's names.
_SUPPORTING = frozenset({"postgresql", "redis", "mariadb", "mysql", "mongodb", "nginx", "rabbitmq",
                         "minio", "influxdb", "caddy"})


def _app_slug(word: str) -> str | None:
    word = word.lower()
    for candidate in (word, word.split("-")[0]):
        slug = _APP_ALIASES.get(candidate, candidate)
        if slug in APP_LOGOS and slug != "docker":
            return slug
    return None


def _image_words(image: str) -> list[str]:
    """`ghcr.io/immich-app/immich-server:v2@sha256:…` → ["immich-server", "immich-app"]."""
    ref = image.split("@")[0]
    parts = ref.split("/")
    parts[-1] = parts[-1].split(":")[0]
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]           # a registry host, not a name
    return [parts[-1]] + parts[-2::-1][:1]


def app_logo(name: str, images: tuple[str, ...] | list[str]) -> str:
    """The logo for a compose project, read off names timar already has — no connection of its
    own. The project's name first, then each image's name and its owner (`vaultwarden/server` is
    Vaultwarden). Project names are often not the application's (`photos`, `vault`), so the
    images usually decide. A supporting service — a database, a proxy — counts only when nothing
    else matched, and with nothing at all it is Docker's whale.

    A project that builds its own image is its own application, whatever runs beside it: compose
    names such an image `<project>-<service>`, and a search engine it ships as a sidecar must
    not become its face. Found on a real fleet, where an app showed the SearXNG logo.
    """
    if own := _app_slug(name):
        return own
    lowered = name.lower()
    if any(_image_words(i)[0].lower().startswith(lowered + "-") for i in images):
        return "docker"
    found = [s for s in (_app_slug(w) for i in images for w in _image_words(i)) if s]
    return next((s for s in found if s not in _SUPPORTING), found[0] if found else "docker")


# The last logo each project's images gave, by host and directory. Without it a project whose
# host is asleep — or that was never created — has no images to read, and its row would turn
# into the whale until the host is back: the same project, two looks, for no reason.
LOGO_FILE = "container-logos.json"
_logo_lock = threading.Lock()


def _remembered_logo(key: str, name: str, images: tuple[str, ...]) -> str:
    with _logo_lock:
        path = config.path(LOGO_FILE)
        try:
            known = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            known = {}      # decoration: a damaged file costs the memory, nothing else
        if not images:
            return known.get(key) or app_logo(name, images)
        logo = app_logo(name, images)
        if known.get(key) != logo:
            known[key] = logo
            try:
                config.write_private(LOGO_FILE, json.dumps(known, indent=2, sort_keys=True))
            except OSError:
                pass
        return logo


_EXITED = re.compile(r"^Exited \((-?\d+)\)")


def parse_ps(output: str) -> dict[str, list[Container]]:
    """`docker ps -a --no-trunc --format json` → containers grouped by compose working dir.

    Containers that compose did not start (no working-directory label) are left out: there is no
    project for them to belong to.
    """
    projects: dict[str, list[Container]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        labels = dict(item.split("=", 1) for item in (row.get("Labels") or "").split(",") if "=" in item)
        working_dir = labels.get(WORKING_DIR_LABEL)
        if not working_dir:
            continue
        match = _EXITED.match(row.get("Status") or "")
        projects.setdefault(working_dir.rstrip("/"), []).append(Container(
            id=row.get("ID", ""),
            name=row.get("Names", ""),
            image=row.get("Image", ""),
            state=(row.get("State") or "").lower(),
            health=(row.get("HealthStatus") or "none").lower(),
            exit_code=int(match.group(1)) if match else None,
        ))
    return projects


def project_state(containers: list[Container], on_demand: bool) -> tuple[str, str]:
    """The project's light and its word.

    A container that exited with code 0 is a one-shot that finished — a migration, an init
    step — and does not make the project degraded. One that exited with anything else, or a
    healthcheck that fails, does: the project answers, but not all of it.
    """
    if not containers:
        return ("asleep", "not created") if on_demand else ("down", "not created")
    running = [c for c in containers if c.state in ("running", "restarting")]
    failed = [c for c in containers
              if c.state in ("dead",) or (c.state == "exited" and c.exit_code not in (0, None))]
    unhealthy = [c for c in running if c.health == "unhealthy"]
    if not running:
        return ("asleep", "stopped") if on_demand else ("down", "stopped")
    if unhealthy:
        return "warn", "unhealthy: " + ", ".join(c.name for c in unhealthy)
    if failed:
        return "warn", "partly down: " + ", ".join(c.name for c in failed)
    if any(c.health == "starting" for c in running):
        return "up", "starting"
    return "up", "running"


def self_container_id() -> str | None:
    """The id of the container this process runs in, if it runs in one.

    Docker bind-mounts the container's own `hostname` and `resolv.conf` from
    `/var/lib/docker/containers/<id>/`, and that path shows in this process's mountinfo — with
    host networking `hostname` is the host's name, so it cannot be used instead.
    """
    try:
        text = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return None
    match = re.search(r"/containers/([0-9a-f]{64})/", text)
    return match.group(1) if match else None


def _ps(server: dict) -> HostContainers:
    command = _DOCKER + '$D ps -a --no-trunc --format json'
    try:
        with connect(server["host"], server["user"], config.resolve_ssh_key(server),
                     timeout=SSH_TIMEOUT) as ssh:
            out, err, code = run(ssh, command, timeout=SSH_TIMEOUT)
    except Exception as e:
        return HostContainers(error=f"could not reach {server['name']}: {e}")
    if code != 0:
        return HostContainers(error=(err.strip() or out.strip() or "docker ps failed")[:200])
    return HostContainers(by_dir=parse_ps(out))


def _host_containers(server: dict, fresh: bool = False) -> HostContainers:
    now = time.monotonic()
    cached = _cache.get(server["name"])
    if cached and not fresh and now - cached[0] < STALE_AFTER:
        return cached[1]
    result = _ps(server)
    _cache[server["name"]] = (time.monotonic(), result)
    return result


def _healthy(url: str, fresh: bool = False) -> bool:
    """Does the service answer? Anything below 500 counts — a login page is a live service.

    Certificates are not verified: this asks whether something answers, not whether it can be
    trusted, and a panel on an internal address with its own certificate is the ordinary case.
    """
    now = time.monotonic()
    cached = _health_cache.get(url)
    if cached and not fresh and now - cached[0] < STALE_AFTER:
        return cached[1]
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT, context=context):
            ok = True
    except urllib.error.HTTPError as e:
        ok = e.code < 500
    except Exception:
        ok = False
    _health_cache[url] = (time.monotonic(), ok)
    return ok


def refresh(cfg: dict) -> None:
    """Read every Docker host with registered projects, and every health address, now.

    Run after `status.refresh`, so whether a host is up is already known: a host that is off is
    not asked over SSH, and its projects' health addresses are not asked either.
    """
    entries = cfg.get("containers") or []
    servers = {s["name"]: s for s in cfg.get("servers", [])}
    hosts = [servers[n] for n in dict.fromkeys(e.get("server") for e in entries)
             if n in servers and fleet_status._probe(servers[n]["host"])]
    urls = list(dict.fromkeys(e["health_url"] for e in entries
                              if e.get("health_url") and e.get("server") in {h["name"] for h in hosts}))
    jobs = [lambda h=h: _host_containers(h, fresh=True) for h in hosts]
    jobs += [lambda u=u: _healthy(u, fresh=True) for u in urls]
    if jobs:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL + 8, len(jobs))) as pool:
            list(pool.map(lambda job: job(), jobs))


def invalidate() -> None:
    _cache.clear()
    _health_cache.clear()


def projects(cfg: dict) -> list[ProjectStatus]:
    entries = cfg.get("containers") or []
    if not entries:
        return []
    servers = {s["name"]: s for s in cfg.get("servers", [])}
    hosts = {e["server"] for e in entries if e.get("server") in servers}
    up = {name: fleet_status._probe(servers[name]["host"]) for name in hosts}
    reachable = [servers[name] for name in hosts if up[name]]

    answers: dict[str, HostContainers] = {}
    if reachable:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(reachable))) as pool:
            for server, answer in zip(reachable, pool.map(_host_containers, reachable)):
                answers[server["name"]] = answer

    # Asked together rather than one after another: fourteen addresses in turn cost the sum of
    # their round trips, in parallel the slowest one. Usually all are cached by `refresh`.
    urls = list(dict.fromkeys(e["health_url"] for e in entries
                              if e.get("health_url") and up.get(e.get("server"))))
    health: dict[str, bool] = {}
    if urls:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL + 8, len(urls))) as pool:
            health = dict(zip(urls, pool.map(_healthy, urls)))

    me = self_container_id()
    out = []
    for entry in entries:
        server_name = entry.get("server", "")
        path = entry.get("path", "").rstrip("/")
        on_demand = bool(entry.get("on_demand"))
        containers: list[Container] = []
        if server_name not in servers:
            state, detail = "down", "host not configured"
        elif not up.get(server_name):
            state, detail = "unknown", "host is not up"
        elif answers[server_name].error:
            state, detail = "unknown", answers[server_name].error
        else:
            containers = answers[server_name].by_dir.get(path, [])
            state, detail = project_state(containers, on_demand)
            if state == "up" and (health_url := entry.get("health_url")) and not health.get(health_url, True):
                state, detail = "warn", "running, but the health check does not answer"
        images = tuple(dict.fromkeys(c.image for c in containers))
        out.append(ProjectStatus(
            name=entry["name"], server=server_name, path=path, state=state, detail=detail,
            images=images, on_demand=on_demand, web_url=entry.get("web_url"),
            is_self=bool(me) and any(c.id == me for c in containers),
            logo=_remembered_logo(f"{server_name}:{path}", entry["name"], images),
        ))
    return sorted(out, key=lambda p: p.name.casefold())


ACTIONS = {"start": "up -d", "stop": "stop", "restart": "restart"}

UPDATE_MODES = ("pull", "custom", "skip")


def update_command(entry: dict, running: bool) -> str:
    """What an update run executes for one project, from its directory.

    Never `down` first. A `down` followed by a failed pull leaves the project removed and off —
    the update script this replaces lost a service for six days that way, when a locally built
    image could not be pulled. Pulling first changes nothing on failure, and `up -d` recreates
    only the containers whose image changed.

    `--ignore-buildable`: services with a `build:` section have no image to pull; asking the
    registry for one fails the whole pull. They are rebuilt by hand, or by a custom command.

    A project that was not running is pulled and left stopped: the run puts the machine back
    the way it found it, and starting an on-demand project can be the very thing that hurts.
    """
    cd = f"cd {shlex.quote(entry['path'])} && "
    if entry.get("update") == "custom":
        return _DOCKER + cd + entry["update_cmd"]
    command = _DOCKER + cd + "$D compose pull --ignore-buildable"
    if running:
        command += " && $D compose up -d"
    return command


def act(entry: dict, servers: list[dict], action: str) -> str:
    """Start, stop or restart one project with `docker compose`, in its own directory.

    `up -d` for start rather than `start`: after a `down` the containers no longer exist, and
    `start` would report success while starting nothing.
    """
    if action not in ACTIONS:
        raise ContainerError(f"unknown action {action!r}")
    server = next((s for s in servers if s["name"] == entry.get("server")), None)
    if server is None:
        raise ContainerError(f"{entry['name']}: its host {entry.get('server')!r} is not configured")
    if not get_platform(server.get("platform")).supports_docker:
        raise ContainerError(f"{server['name']} does not run Docker")
    me = self_container_id()
    if me and action != "start":
        answer = _host_containers(server)
        if any(c.id == me for c in answer.by_dir.get(entry.get("path", "").rstrip("/"), [])):
            raise ContainerError(f"{entry['name']} is timar itself — stopping it from here would "
                                 "take this page down with it")
    command = (_DOCKER + f"cd {shlex.quote(entry['path'])} && $D compose {ACTIONS[action]} 2>&1")
    try:
        with connect(server["host"], server["user"], config.resolve_ssh_key(server),
                     timeout=SSH_TIMEOUT) as ssh:
            out, err, code = run(ssh, command, timeout=300)
    except Exception as e:
        raise ContainerError(f"could not reach {server['name']}: {e}") from e
    if code != 0:
        raise ContainerError(f"{entry['name']}: {(out.strip() or err.strip() or 'docker compose failed')[-300:]}")
    return f"{entry['name']}: {action} done"


_TRAEFIK_HOST = re.compile(r"traefik\.http\.routers\.[^.=,]+\.rule=Host\(`([^`]+)`\)")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Found:
    """A compose project on a host, as offered for import."""
    name: str
    path: str
    status: str             # compose's own word: "running(4)", "exited(1)"
    web_url: str | None     # from a Traefik Host() rule on one of its containers, if any


def parse_discovery(ls_output: str, ps_output: str) -> list[Found]:
    """`docker compose ls -a --format json` + `docker ps -a --format json` → projects to offer.

    The directory is that of the project's first compose file — the working directory Docker
    records, which is what the containers page matches on. The web address is a guess from the
    first Traefik `Host()` rule found on the project's containers; the form shows it to be kept
    or cleared, never applied unseen.
    """
    try:
        projects = json.loads(ls_output or "[]")
    except ValueError:
        return []
    hosts: dict[str, str] = {}
    for line in ps_output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        labels = row.get("Labels") or ""
        match = re.search(re.escape(WORKING_DIR_LABEL) + r"=([^,]+)", labels)
        host = _TRAEFIK_HOST.search(labels)
        if match and host:
            hosts.setdefault(match.group(1).rstrip("/"), host.group(1))
    found = []
    for project in projects:
        files = (project.get("ConfigFiles") or "").split(",")
        if not files or not files[0].startswith("/"):
            continue
        path = files[0].rsplit("/", 1)[0] or "/"
        name = _NAME_UNSAFE.sub("-", project.get("Name") or path.rsplit("/", 1)[-1]).strip("-") or "project"
        host = hosts.get(path)
        found.append(Found(name=name, path=path, status=project.get("Status") or "",
                           web_url=f"https://{host}" if host else None))
    return sorted(found, key=lambda f: f.name.casefold())


def discover(server: dict) -> list[Found]:
    """The compose projects on one host, read over SSH. Raises ContainerError with the reason."""
    if not get_platform(server.get("platform")).supports_docker:
        raise ContainerError(f"{server['name']} does not run Docker")
    command = _DOCKER + '$D compose ls -a --format json && echo "---timar---" && $D ps -a --no-trunc --format json'
    try:
        with connect(server["host"], server["user"], config.resolve_ssh_key(server),
                     timeout=SSH_TIMEOUT) as ssh:
            out, err, code = run(ssh, command, timeout=60)
    except Exception as e:
        raise ContainerError(f"could not reach {server['name']}: {e}") from e
    if code != 0 or "---timar---" not in out:
        raise ContainerError((err.strip() or out.strip() or "docker compose ls failed")[-300:])
    ls_output, ps_output = out.split("---timar---", 1)
    return parse_discovery(ls_output.strip(), ps_output)


SORT_KEYS = ("name", "host")


def sort_projects(projects: list[ProjectStatus], key: str, descending: bool = False) -> list[ProjectStatus]:
    """By name, or by host and then directory; ties fall back to the name so the order is stable."""
    by_name = sorted(projects, key=lambda p: p.name.casefold())
    if key == "host":
        by_name = sorted(by_name, key=lambda p: (p.server.casefold(), p.path))
    return by_name[::-1] if descending else by_name
