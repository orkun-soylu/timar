"""Which servers a job runs on: enrolled, and not left out of it.

Two records, kept apart on purpose:

- **Enrolled** — Timar has logged in to the address with its own key at least once. Written by
  every key login (`ssh.connect`, `enroll.verify`), in `ssh/enrolled` beside `known_hosts`: an
  append-only list of addresses, one line each, under a lock. Not in the state file, which the
  scheduler rewrites — a background login racing a job's own write must not cost either.
  Installations that predate the record start from `known_hosts`: an address Timar pinned a host
  key for is one it has connected to.
- **Left out** — `job_exclude` in `config.yaml`, one list of server names per job, set from the
  job's dialog. Hand-editable like the rest of the file.

A job runs on a server that is enrolled and not left out. The others are not woken and not
connected to; the report says why.
"""
from __future__ import annotations

import threading

from . import config
from .network import split_address

ENROLLED = "ssh/enrolled"
EXCLUDE = "job_exclude"

_lock = threading.Lock()
# Keyed by the file's path, so a different data directory (a test, a second installation in the
# same process) never sees another one's record.
_cache: dict[str, set[str]] = {}


def address_key(address: str) -> str:
    """The address as `known_hosts` writes it: `host`, or `[host]:port` off port 22."""
    host, port = split_address(address)
    return host if port == 22 else f"[{host}]:{port}"


def _from_known_hosts() -> set[str]:
    path = config.path("ssh/known_hosts")
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            names.update(line.split()[0].split(","))
    return names


def enrolled() -> set[str]:
    """Every address Timar has logged in to with its key."""
    path = config.path(ENROLLED)
    with _lock:
        if str(path) not in _cache:
            if path.exists():
                found = {l.strip() for l in path.read_text().splitlines() if l.strip()}
            else:
                # First run with this record: everything already pinned counts as reached.
                found = _from_known_hosts()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(f"{a}\n" for a in sorted(found)))
            _cache[str(path)] = found
        return set(_cache[str(path)])


def mark_enrolled(address: str) -> None:
    """Record a key login. Cheap when already known — it is called on every connection."""
    key = address_key(address)
    if key in enrolled():
        return
    with _lock:
        path = config.path(ENROLLED)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"{key}\n")
        _cache.setdefault(str(path), set()).add(key)


def is_enrolled(server: dict) -> bool:
    return address_key(server["host"]) in enrolled()


def excluded(cfg: dict, job: str) -> set[str]:
    return set((cfg.get(EXCLUDE) or {}).get(job) or [])


def skip_reason(cfg: dict, job: str, server: dict) -> str | None:
    """Why a job does not run on this server, or None when it does."""
    if not config.has_ssh(server):
        return "watched only, no SSH"
    if server["name"] in excluded(cfg, job):
        return "left out of this job"
    if not is_enrolled(server):
        return "not enrolled — enrol it from its server card"
    return None


def lists(cfg: dict, job: str) -> tuple[list[dict], list[dict], list[dict]]:
    """The job dialog's three lists: runs on, left out (enrolled), not enrolled."""
    out = excluded(cfg, job)
    runs, left, unenrolled = [], [], []
    for server in sorted(cfg.get("servers", []), key=lambda s: s["name"].casefold()):
        if not config.has_ssh(server):
            continue    # not a member of any job, and nothing to enrol: not listed at all
        if not is_enrolled(server):
            unenrolled.append(server)
        elif server["name"] in out:
            left.append(server)
        else:
            runs.append(server)
    return runs, left, unenrolled


def set_excluded(cfg: dict, job: str, name: str, leave_out: bool) -> None:
    lists_ = dict(cfg.get(EXCLUDE) or {})
    names = set(lists_.get(job) or [])
    (names.add if leave_out else names.discard)(name)
    lists_[job] = sorted(names)
    lists_ = {k: v for k, v in lists_.items() if v}
    # Nothing left out anywhere: no key at all, so a config that never used this stays as it was.
    if lists_:
        cfg[EXCLUDE] = lists_
    else:
        cfg.pop(EXCLUDE, None)


def rename(cfg: dict, old: str, new: str) -> None:
    for names in (cfg.get(EXCLUDE) or {}).values():
        if old in names:
            names[names.index(old)] = new


def forget(cfg: dict, name: str) -> None:
    for job, names in list((cfg.get(EXCLUDE) or {}).items()):
        if name in names:
            names.remove(name)


def reset_cache() -> None:
    """For tests: the next read goes back to the file."""
    with _lock:
        _cache.clear()
