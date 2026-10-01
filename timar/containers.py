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

TTL = 10.0
MAX_PARALLEL = 8
SSH_TIMEOUT = 15
HEALTH_TIMEOUT = 4.0
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"

# Plain `docker` when the account is in the docker group, `sudo -n docker` when it is not —
# decided on the host, so an operator does not have to tell Timar which of the two applies.
_DOCKER = 'if docker info >/dev/null 2>&1; then D=docker; else D="sudo -n docker"; fi; '

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

    @property
    def running(self) -> bool:
        return self.state in ("up", "warn")


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


def _host_containers(server: dict) -> HostContainers:
    now = time.monotonic()
    cached = _cache.get(server["name"])
    if cached and now - cached[0] < TTL:
        return cached[1]
    result = _ps(server)
    _cache[server["name"]] = (now, result)
    return result


def _healthy(url: str) -> bool:
    """Does the service answer? Anything below 500 counts — a login page is a live service.

    Certificates are not verified: this asks whether something answers, not whether it can be
    trusted, and a panel on an internal address with its own certificate is the ordinary case.
    """
    now = time.monotonic()
    cached = _health_cache.get(url)
    if cached and now - cached[0] < TTL:
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
    _health_cache[url] = (now, ok)
    return ok


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
            if state == "up" and (health_url := entry.get("health_url")) and not _healthy(health_url):
                state, detail = "warn", "running, but the health check does not answer"
        images = tuple(dict.fromkeys(c.image for c in containers))
        out.append(ProjectStatus(
            name=entry["name"], server=server_name, path=path, state=state, detail=detail,
            images=images, on_demand=on_demand, web_url=entry.get("web_url"),
            is_self=bool(me) and any(c.id == me for c in containers),
        ))
    return sorted(out, key=lambda p: p.name.casefold())


ACTIONS = {"start": "up -d", "stop": "stop", "restart": "restart"}


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
            raise ContainerError(f"{entry['name']} is Timar itself — stopping it from here would "
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
