"""The `/data` volume: everything an installation is, in one directory.

```
/data/
  config.yaml     servers, schedules, LLM and notifier settings
  auth.json       the operator's username and password hash
  secret_key      signs session cookies
  state.json      last run results, heartbeats
  notes.md        operator-authored standing context for the log analysis
  ssh/id_ed25519  the key this installation presents to every managed host
```

One directory means backup and migration are `cp -a`, which is the whole reason the layout is
flat and the paths are not configurable individually.

**Nothing here ships in the image.** A fresh container starts with an empty volume and no
config at all; `load()` returning an empty dict is the first-run state, not an error.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import yaml


def data_dir() -> Path:
    """`TIMAR_DATA`, else `/data` in a container, else `./data` for development."""
    if env := os.environ.get("TIMAR_DATA"):
        return Path(env)
    if Path("/data").is_dir():
        return Path("/data")
    return Path.cwd() / "data"


def path(name: str) -> Path:
    return data_dir() / name


CONFIG = "config.yaml"
AUTH = "auth.json"
SECRET_KEY = "secret_key"
STATE = "state.json"
NOTES = "notes.md"
SSH_KEY = "ssh/id_ed25519"


def ensure_dir() -> Path:
    d = data_dir()
    (d / "ssh").mkdir(parents=True, exist_ok=True)
    return d


def write_private(name: str, content: str) -> Path:
    """Write a file only the owner can read, replacing any previous version atomically.

    Atomic because a half-written `config.yaml` is an installation that will not start, and the
    web layer rewrites it while the scheduler may be reading it. The temporary file is created
    in the *same* directory so `os.replace` stays within one filesystem — `/tmp` is a different
    mount inside the container and the rename would fail.

    `0600` before the rename, not after: between a world-readable create and a later chmod there
    is a window in which `auth.json` or the SSH key is readable, and that window is exactly what
    an attacker with a shell would wait for.
    """
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def load() -> dict:
    """The installation's configuration, or `{}` when it has not been set up yet."""
    p = path(CONFIG)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def save(cfg: dict) -> Path:
    return write_private(CONFIG, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def load_notes() -> str:
    p = path(NOTES)
    return p.read_text() if p.exists() else ""


def resolve_ssh_key(server: dict) -> str:
    """The key to use for a server: its own if it names one, otherwise this installation's.

    Per-server keys exist for hosts enrolled before Timar, or ones an operator wants isolated.
    The common case is the generated key, so it is the default rather than a required field.
    """
    if key := server.get("ssh_key"):
        return os.path.expanduser(key)
    return str(path(SSH_KEY))


def on_demand(servers: list[dict]) -> dict[str, str]:
    """Which servers are expected to be off, and why. Absent from the map means always on.

    Two ways a machine earns it, and both must be honoured everywhere or the fleet is described
    inconsistently:

    1. It has a `wol_mac` — a machine with a wake address is by definition one meant to sleep.
       Value: `"wol"`.
    2. **Its hypervisor is on-demand.** A guest has no wake address of its own — it cannot have
       one, it is started by `qm` — so keying off `wol_mac` alone calls a sleeping VM always-on
       and every sweep then reports it as an outage. Value: the hypervisor's name.

    Inherited rather than granted to every guest, because the two mistakes are not equal. Calling
    an on-demand guest always-on produces a nightly false alarm, which is merely noise; calling a
    guest of an always-on host on-demand normalises its outage, so a VM that has actually crashed
    is reported as sleeping soundly. Inheritance is also transitive — nested virtualisation is
    unusual but the fixpoint costs nothing and the alternative is a wrong answer at depth two.

    Lives here, not in each caller, because it was written out three times and the third copy
    dropped the guest clause: the dashboard and the analysis agreed a Kali VM was on-demand while
    the settings page called it always on.
    """
    reasons = {s["name"]: "wol" for s in servers if s.get("wol_mac")}
    guests = [(host["name"], guest["server_name"])
              for host in servers for guest in host.get("manages_vms", [])]

    changed = True
    while changed:
        changed = False
        for hypervisor, guest in guests:
            if hypervisor in reasons and guest not in reasons:
                reasons[guest] = hypervisor
                changed = True
    return reasons


def is_configured() -> bool:
    """Has an operator account been created? Everything else is optional; this is not."""
    return path(AUTH).exists()
