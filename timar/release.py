"""timar's own version: which one this is, whether a newer one is out, and moving to it.

**The check is one request to GitHub's releases API, made when the settings page is opened**
and remembered for hours — not a background poll. A tool whose whole point is reaching only the
machines it was told about should not phone home on a timer; `check_for_updates: false` in
`config.yaml` turns even this off.

**The upgrade is handed to the host, like the host's own package update** (see `updater`): the
container that starts it is the one being replaced, so anything run from inside this process
dies half-way. The host's systemd runs four steps, each of which stops the rest on failure:

1. pull the new image — nothing has changed yet if this fails;
2. if the compose file pins the running version, rewrite that tag to the new one;
3. `docker compose up -d` for timar's service only;
4. record the exit status, which the restarted timar reads back.

A pinned tag is rewritten rather than left alone because the pin is the operator's choice of
"upgrades happen when I say so", and pressing the button is saying so. Any other tag — a local
build, a `major.minor` channel — is not this button's to move, and the upgrade is refused before
anything runs.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import threading
import time
from datetime import datetime
from importlib import metadata

import httpx

from . import config, containers, state, updater
from .network import is_host_up
from .ssh import connect, run

logger = logging.getLogger(__name__)

REPOSITORY = "orkun-soylu/timar"
SOURCE_URL = f"https://github.com/{REPOSITORY}"
SITE_URL = "https://timar.tools"
IMAGE = f"ghcr.io/{REPOSITORY}"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"

# Unauthenticated, the API allows 60 requests an hour per address. A failed check is remembered
# for a shorter time so a network blip does not hide a release for the rest of the day.
CHECK_EVERY = 6 * 3600
RETRY_AFTER = 15 * 60
CHECK_TIMEOUT = 5.0

UPGRADE_UNIT = "timar-upgrade"
UPGRADE_LOG = "/var/log/timar-upgrade.log"
UPGRADE_RC = "/var/log/timar-upgrade.rc"
# Long enough for the page to get its answer before the container goes.
UPGRADE_DELAY = 3
# A pull over a slow line plus a restart; past this the upgrade is reported as lost.
UPGRADE_GIVE_UP = 15 * 60

_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")
_cache: dict = {}
_lock = threading.Lock()


class UpgradeError(RuntimeError):
    """Why the upgrade was not started, in words for the operator."""


def current_version() -> str:
    try:
        return metadata.version("timar")
    except metadata.PackageNotFoundError:
        return "unknown"


def parse(version: str | None) -> tuple[int, ...] | None:
    match = _VERSION.match((version or "").strip())
    return tuple(int(p) for p in match.group(1).split(".")) if match else None


def is_newer(candidate: str | None, than: str | None) -> bool:
    new, old = parse(candidate), parse(than)
    return bool(new and old and new > old)


def check_enabled(cfg: dict) -> bool:
    return cfg.get("check_for_updates", True) is not False


def latest(fresh: bool = False) -> dict | None:
    """The newest published release as {"version", "url", "name"}, or None if unknown.

    `/releases/latest` already leaves out drafts and pre-releases.
    """
    with _lock:
        now = time.monotonic()
        if not fresh and _cache and now - _cache["at"] < _cache["ttl"]:
            return _cache["release"]
        release, ttl = None, RETRY_AFTER
        try:
            response = httpx.get(RELEASES_API, timeout=CHECK_TIMEOUT,
                                 headers={"Accept": "application/vnd.github+json"})
            response.raise_for_status()
            body = response.json()
            tag = body.get("tag_name") or ""
            if parse(tag):
                release = {"version": tag.lstrip("v"), "url": body.get("html_url") or SOURCE_URL,
                           "name": body.get("name") or tag}
                ttl = CHECK_EVERY
        except (httpx.HTTPError, ValueError) as e:
            logger.info("could not check for a newer timar: %s", e)
        _cache.update(at=now, ttl=ttl, release=release)
        return release


# -- the upgrade -------------------------------------------------------------------------------

def _split_image(image: str) -> tuple[str, str]:
    """`repo:tag` → (repo, tag); no tag is `latest`. A registry port is not a tag."""
    name, _, tag = image.rpartition(":")
    if not name or "/" in tag:
        return image, "latest"
    return name, tag


def _bre(text: str) -> str:
    """`text` as a literal in a sed basic regular expression delimited by `#`."""
    return re.sub(r"([.\[\]*^$\\#])", r"\\\1", text)


def upgrade_command(info: dict, target: str, running: str) -> str:
    """The shell the host runs, as root, under its own systemd. See the module docstring.

    `info` is what `docker inspect` says about this container: its image and compose labels.
    """
    labels = info.get("Config", {}).get("Labels") or {}
    workdir = labels.get("com.docker.compose.project.working_dir")
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    files = [f for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f]
    if not (workdir and project and service and files):
        raise UpgradeError("timar was not started with docker compose, so there is no compose "
                           "file to upgrade it from — pull the new image by hand.")
    repo, tag = _split_image(info.get("Config", {}).get("Image") or "")
    if repo != IMAGE:
        raise UpgradeError(f"timar runs from {repo}, not the published image {IMAGE} — "
                           "a build of your own is upgraded by rebuilding it.")
    q = shlex.quote
    compose = f"docker compose -p {q(project)} " + " ".join(f"-f {q(f)}" for f in files)
    steps = [f"cd {q(workdir)}"]
    if tag == "latest":
        steps += [f"{compose} pull {q(service)}"]
    elif tag == running:
        old, new = f"{repo}:{tag}", f"{repo}:{target}"
        listed = " ".join(q(f) for f in files)
        # Up to a character that cannot continue a tag, so 0.2.2 does not also match 0.2.27.
        script = "s#" + _bre(old) + r"\([^0-9A-Za-z._-]\|$\)#" + new + r"\1#g"
        steps += [f"docker pull {q(new)}",
                  f'{{ grep -qF {q(old)} {listed} || {{ echo "no compose file names {old}"; exit 3; }}; }}',
                  f"sed -i {q(script)} {listed}"]
    else:
        raise UpgradeError(f"the compose file asks for {repo}:{tag}, which a button should not "
                           "move — change the tag by hand.")
    steps += [f"{compose} up -d {q(service)}"]
    return " && ".join(steps)


def _inspect_self(ssh, me: str) -> dict:
    out, err, code = run(ssh, containers.DOCKER + f"$D inspect {me}", timeout=30)
    if code != 0:
        raise UpgradeError((err.strip() or out.strip() or "docker inspect failed")[:300])
    try:
        return json.loads(out)[0]
    except (ValueError, IndexError) as e:
        raise UpgradeError(f"could not read docker inspect: {e}") from e


def _server(cfg: dict, name: str) -> dict:
    return next(s for s in cfg.get("servers", []) if s["name"] == name)


def start_upgrade(cfg: dict, target: str) -> None:
    """Hand the upgrade to timar's own host. Raises UpgradeError when it cannot be started."""
    running = current_version()
    if not is_newer(target, running):
        raise UpgradeError(f"{target} is not newer than {running}.")
    me = containers.self_container_id()
    if not me:
        raise UpgradeError("timar is not running in a container.")
    host = updater.find_self_host(cfg)
    if not host:
        raise UpgradeError("timar could not find the host it runs on. Add its compose project "
                           "on the containers page, on the server it runs on.")
    server = _server(cfg, host)
    unit = f"{UPGRADE_UNIT}-{datetime.now():%Y%m%d%H%M%S}"
    with connect(server["host"], server["user"], config.resolve_ssh_key(server)) as ssh:
        command = upgrade_command(_inspect_self(ssh, me), target, running)
        out, err, code = run(ssh, updater.self_update_command(
            command, unit, log=UPGRADE_LOG, rc=UPGRADE_RC, delay=UPGRADE_DELAY), timeout=60)
    if code != 0:
        raise UpgradeError(updater.failure_detail(out, err))
    logger.info("upgrade to %s handed to %s on %s", target, unit, host)
    data = state.load()
    data["upgrade"] = {"from": running, "target": target, "host": host, "unit": unit,
                       "started": time.time()}
    state.save(data)


def upgrade_status(cfg: dict) -> dict | None:
    """Where a started upgrade stands, or None if none was started.

    {"state": "done" | "pending" | "failed", "target", "error"}. A finished or failed upgrade is
    reported once and then forgotten.
    """
    data = state.load()
    pending = data.get("upgrade")
    if not pending:
        return None
    target = pending.get("target", "")
    result = {"state": "pending", "target": target, "error": ""}
    if current_version() == target:
        result["state"] = "done"
    elif time.time() - pending.get("started", 0) > UPGRADE_GIVE_UP:
        result.update(state="failed", error=f"no answer after {UPGRADE_GIVE_UP // 60} minutes — "
                                            f"see {UPGRADE_LOG} on {pending.get('host')}")
    else:
        try:
            server = _server(cfg, pending["host"])
            if is_host_up(server["host"]):
                with connect(server["host"], server["user"], config.resolve_ssh_key(server)) as ssh:
                    word, rc, log = updater.handoff_status(ssh, pending["unit"],
                                                           log=UPGRADE_LOG, rc=UPGRADE_RC)
                if word == "done" and rc != 0:
                    result.update(state="failed",
                                  error=f"exit {rc}: {updater._tail(log) or 'no output'}")
                elif word == "gone":
                    result.update(state="failed",
                                  error=f"{pending['unit']} stopped without reporting back")
                # done with 0 and the old version still answering: this process is about to be
                # replaced. Still pending.
        except StopIteration:
            result.update(state="failed", error=f"{pending.get('host')} is no longer configured")
        except Exception as e:
            logger.info("could not check the upgrade yet: %s", e)
    if result["state"] != "pending":
        data.pop("upgrade", None)
        state.save(data)
    return result
