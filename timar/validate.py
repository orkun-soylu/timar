"""Validation for operator-supplied configuration.

Pure functions, deliberately separate from the web layer: the same rules have to hold for a
config file written by hand, and a rule that only lives in a form handler does not.

Errors are collected rather than raised one at a time — a form that reports its first problem,
then the next one after you fix it, is a form people learn to dread.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .i18n import gettext as _
from .network import split_address
from .platforms import PLATFORMS

MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MIN_UPDATE_TIMEOUT = 60
MAX_UPDATE_TIMEOUT = 14400  # 4 hours


class ValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# Every key `server()` below can produce — that is, everything the server form owns. A caller
# editing an entry needs this to tell the fields it may replace from the ones it must carry
# across untouched, and it lives here rather than at the call site so the two cannot drift: a
# field added to the form and forgotten in a copy of this set would be treated as hand-written,
# and clearing it in the form would silently never stick.
#
# `wol_broadcast` and `wol_relay` are deliberately not here: they are advanced settings written in
# `config.yaml` by hand (see config.example.yaml), and leaving them out is what makes an edit in
# the form carry them across untouched instead of dropping them.
SERVER_FIELDS = frozenset({
    "name", "host", "user", "platform", "wol_mac",
    "update_cmd", "context", "update_timeout", "on_demand", "web_url",
})


def server(form: dict, existing_names: set[str], original_name: str | None = None,
           schemes: frozenset[str] = frozenset()) -> dict:
    """Validate and normalise one server entry.

    `original_name` is the name being edited, so renaming a server to itself is not a clash.
    `schemes` are the extra link schemes `config.yaml` allows; see `link_schemes`.
    """
    errors: list[str] = []
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    user = (form.get("user") or "").strip()
    platform = (form.get("platform") or "").strip()

    if not name:
        errors.append(_("Name is required."))
    elif not NAME.match(name):
        # The name is used in report text, log lines and as a dictionary key; keeping it to
        # plain characters avoids a whole class of quoting surprises later.
        errors.append(_("Name may contain only letters, digits, dot, dash and underscore."))
    elif name != original_name and name in existing_names:
        errors.append(_("A server named {name!r} already exists.", name=name))

    if not host:
        errors.append(_("Address is required."))
    else:
        try:
            port = split_address(host)[1]
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            errors.append(_("Address must be a host, optionally with an SSH port: 10.0.0.5 or 10.0.0.5:2222."))
    if not user:
        errors.append(_("SSH user is required."))
    if platform not in PLATFORMS:
        errors.append(_("Platform must be one of: {options}.", options=", ".join(PLATFORMS)))

    entry: dict = {"name": name, "host": host, "user": user, "platform": platform}

    if mac := (form.get("wol_mac") or "").strip():
        if not MAC.match(mac):
            errors.append(_("Wake-on-LAN MAC must look like aa:bb:cc:dd:ee:ff."))
        else:
            entry["wol_mac"] = mac.lower().replace("-", ":")

    # Only without a MAC: with one the machine is on-demand already, and the flag would be a
    # second switch that does nothing. On a guest the same box is written to the hypervisor's
    # `manages_vms` entry instead — see `settings._relink_guest`.
    if form.get("on_demand") and not entry.get("wol_mac") and not (form.get("hypervisor") or "").strip():
        entry["on_demand"] = True

    for optional in ("update_cmd", "context"):
        if value := (form.get(optional) or "").strip():
            entry[optional] = value

    if raw_url := (form.get("web_url") or "").strip():
        if url := web_url(raw_url, schemes):
            entry["web_url"] = url
        elif scheme := _unlisted_scheme(raw_url):
            # The one rejection with a fix the operator cannot guess: it is not in the form.
            errors.append(_("Web interface: {scheme}:// links are not allowed. To allow them, add "
                            "{scheme} to link_schemes in config.yaml.", scheme=scheme))
        else:
            errors.append(_("Web interface must be an address such as 10.0.0.5:8006 or https://host.lan."))

    _update_timeout(form, entry, errors)

    if errors:
        raise ValidationError(errors)
    return entry


# Never a link, whatever `link_schemes` says. Each of these runs or reads something inside the
# page's own session instead of handing the address to another program, and this page sits in
# front of a key that reaches every machine in the fleet.
NEVER_LINK = frozenset({"javascript", "vbscript", "data", "file", "blob", "about", "filesystem"})
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")


def link_schemes(cfg: dict) -> frozenset[str]:
    """The extra schemes a server's web interface may use: `link_schemes` in `config.yaml`.

    For a link that opens a program rather than a page — a terminal handler registered on the
    operator's machine, say `claude://open`. Only from the file, not the form: allowing a scheme
    is a decision about what this page may launch, and it is made once, deliberately, by whoever
    owns the installation. Entries are lower-cased and a trailing `:` or `://` is forgiven.
    """
    raw = cfg.get("link_schemes") or []
    if isinstance(raw, str):
        raw = [raw]
    found = {str(s).strip().lower().rstrip(":/") for s in raw}
    return frozenset(s for s in found if _SCHEME.match(s)) - NEVER_LINK - {"http", "https"}


def _unlisted_scheme(raw: str) -> str | None:
    """The scheme of a `name://...` value that is neither http(s) nor ruled out for good."""
    scheme = raw.split("://", 1)[0].lower() if "://" in raw else ""
    if _SCHEME.match(scheme) and scheme not in NEVER_LINK | {"http", "https"}:
        return scheme
    return None


def web_url(raw: str, schemes: frozenset[str] = frozenset()) -> str | None:
    """The address a server's own web interface answers on, or `None` when it is not one.

    A bare address gets `https://`: the panels this is for — a hypervisor, a router, anything
    behind a reverse proxy — answer on https, and one that answers on plain http says so by
    being typed with its scheme. Only http and https are let through by default, because the
    value ends up in an `href`, where `javascript:` would run in the operator's session.
    `schemes` adds the ones `config.yaml` allows (see `link_schemes`); those need no host —
    `claude://` alone is a whole link to a program.
    """
    raw = raw.strip()
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
        if scheme in schemes and scheme not in NEVER_LINK:
            # No whitespace or control characters: a link that is not one string is not a link.
            return raw if not any(c.isspace() or ord(c) < 32 for c in raw) else None
    url = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urlsplit(url)
        parts.port  # raises on a port that is not a number
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        return None
    return url


CONTAINER_FIELDS = frozenset({"name", "server", "path", "on_demand", "web_url", "health_url",
                              "update", "update_cmd", "update_timeout"})


def container(form: dict, existing_names: set[str], servers: list[dict],
              original_name: str | None = None) -> dict:
    """Validate one compose project entry. Same rules as `server` where the two overlap."""
    errors: list[str] = []
    name = (form.get("name") or "").strip()
    server_name = (form.get("server") or "").strip()
    path = (form.get("path") or "").strip()

    if not name:
        errors.append(_("Name is required."))
    elif not NAME.match(name):
        errors.append(_("Name may contain only letters, digits, dot, dash and underscore."))
    elif name != original_name and name in existing_names:
        errors.append(_("A container named {name!r} already exists.", name=name))

    host = next((s for s in servers if s["name"] == server_name), None)
    if host is None:
        errors.append(_("Host must be one of the configured servers."))
    elif not PLATFORMS[host.get("platform", "linux")].supports_docker:
        errors.append(_("{host!r} does not run Docker.", host=server_name))

    # Absolute, because it is where `docker compose` runs over SSH and is matched against the
    # working-directory label Docker records — which is always absolute. A `~` would be
    # neither expanded nor matched.
    if not path.startswith("/") or any(c in path for c in "\n\r\0"):
        errors.append(_("Compose directory must be an absolute path, such as /srv/immich."))
    path = path.rstrip("/") or "/"

    entry: dict = {"name": name, "server": server_name, "path": path}
    if form.get("on_demand"):
        entry["on_demand"] = True
    for key, label in (("web_url", _("Web interface")), ("health_url", _("Health check"))):
        if raw := (form.get(key) or "").strip():
            if url := web_url(raw):
                entry[key] = url
            else:
                errors.append(_("{field} must be an address such as 10.0.0.5:8080 or https://host.lan.", field=label))

    # `pull` is the default and is not written, so an entry that never chose stays minimal.
    mode = (form.get("update") or "pull").strip()
    if mode not in ("pull", "custom", "skip"):
        errors.append(_("Update must be one of: {options}.", options="pull, custom, skip"))
    elif mode == "custom":
        if cmd := (form.get("update_cmd") or "").strip():
            entry["update"], entry["update_cmd"] = "custom", cmd
        else:
            errors.append(_("A custom update needs a command."))
    elif mode == "skip":
        entry["update"] = "skip"
    _update_timeout(form, entry, errors)

    if errors:
        raise ValidationError(errors)
    return entry


def _update_timeout(form: dict, entry: dict, errors: list[str]) -> None:
    if raw_timeout := (form.get("update_timeout") or "").strip():
        try:
            seconds = int(raw_timeout)
        except ValueError:
            errors.append(_("Update timeout must be a whole number of seconds."))
        else:
            # The lower bound is not fussiness. A timeout below a minute cannot outlast an
            # `apt-get update` on a slow link, so it would fail every run while looking like a
            # deliberate setting; the upper bound keeps one wedged host from holding the
            # sequential fleet walk for most of a day.
            if not MIN_UPDATE_TIMEOUT <= seconds <= MAX_UPDATE_TIMEOUT:
                errors.append(_(
                    "Update timeout must be between {low} and {high} seconds.",
                    low=MIN_UPDATE_TIMEOUT, high=MAX_UPDATE_TIMEOUT))
            else:
                entry["update_timeout"] = seconds


def guest_link(form: dict, servers: list[dict], name: str,
               original_name: str | None = None) -> tuple[str, int] | None:
    """Which hypervisor starts this server, and as which VM id. `None` when it is standalone.

    Stored on the *hypervisor* (`manages_vms`) rather than on the guest, because that is the
    entry the updater walks when it wakes a host and works through what that host is responsible
    for. The form asks the question from the guest's side because that is where an operator is
    standing when they notice the answer is missing.
    """
    hypervisor = (form.get("hypervisor") or "").strip()
    raw_id = (form.get("vm_id") or "").strip()

    if not hypervisor:
        if raw_id:
            raise ValidationError([_("A VM id needs a hypervisor to go with it.")])
        return None

    errors: list[str] = []
    host = next((s for s in servers if s["name"] == hypervisor), None)
    if host is None:
        errors.append(_("Hypervisor {hypervisor!r} is not a configured server.", hypervisor=hypervisor))
    elif host.get("platform") != "proxmox":
        errors.append(_("{hypervisor!r} is not a hypervisor — its platform is {platform!r}.",
                        hypervisor=hypervisor, platform=host.get("platform", "linux")))
    # `original_name` is the entry being edited: it still carries the old name in the stored
    # config, so without it a rename collides with the very link it is renaming.
    own_names = {name, original_name}
    if hypervisor in own_names:
        errors.append(_("A server cannot be its own hypervisor."))

    vm_id = 0
    if not raw_id:
        errors.append(_("A VM id is required when a hypervisor is set."))
    else:
        try:
            vm_id = int(raw_id)
        except ValueError:
            errors.append(_("VM id must be a whole number."))
        else:
            if vm_id < 1:
                errors.append(_("VM id must be a positive number."))
            else:
                clash = next(
                    (g for s in servers if s["name"] == hypervisor
                     for g in s.get("manages_vms", [])
                     if g["vm_id"] == vm_id and g["server_name"] not in own_names),
                    None,
                )
                if clash:
                    errors.append(_("VM {vm_id} on {hypervisor} is already {guest!r}.",
                                    vm_id=vm_id, hypervisor=hypervisor,
                                    guest=clash["server_name"]))

    if errors:
        raise ValidationError(errors)
    return hypervisor, vm_id


def log_check(form: dict) -> dict:
    errors: list[str] = []
    try:
        hours = int(form.get("journal_hours", 6))
        if not 1 <= hours <= 168:
            errors.append(_("Log window must be between 1 and 168 hours."))
    except (TypeError, ValueError):
        errors.append(_("Log window must be a whole number of hours."))
        hours = 6

    try:
        threshold = int(form.get("disk_threshold", 85))
        if not 50 <= threshold <= 99:
            # Below 50 every machine is a finding and the report becomes noise; at 100 a full
            # disk is reported only once it is too late to act on.
            errors.append(_("Disk threshold must be between 50 and 99 percent."))
    except (TypeError, ValueError):
        errors.append(_("Disk threshold must be a whole number."))
        threshold = 85

    if errors:
        raise ValidationError(errors)
    return {"journal_hours": hours, "disk_threshold": threshold}


def llm(form: dict, existing: dict | None) -> dict | None:
    """Validate the model connection. An empty provider clears it, which is a valid state."""
    from .llm import PROVIDERS

    provider = (form.get("provider") or "").strip()
    if not provider:
        return None

    errors: list[str] = []
    if provider not in PROVIDERS:
        errors.append(_("Provider must be one of: {options}.", options=", ".join(PROVIDERS)))

    entry: dict = {"provider": provider}
    if model := (form.get("model") or "").strip():
        entry["model"] = model
    if base_url := (form.get("base_url") or "").strip():
        entry["base_url"] = base_url

    # An empty key field means "leave it as it was", never "delete it". The form cannot show the
    # stored value — it is never sent to the browser — so a blank box is the normal state of the
    # field on every visit, and treating that as a deletion would wipe the key on any unrelated
    # edit. Clearing is done by changing the provider.
    key = (form.get("api_key") or "").strip()
    if key:
        entry["api_key"] = key
    elif existing and existing.get("provider") == provider and existing.get("api_key"):
        entry["api_key"] = existing["api_key"]

    if provider == "anthropic" and not entry.get("api_key"):
        errors.append(_("An API key is required for Anthropic."))

    if errors:
        raise ValidationError(errors)
    return entry


def telegram(form: dict, existing: dict | None) -> dict | None:
    chat_id = (form.get("chat_id") or "").strip()
    token = (form.get("token") or "").strip()
    if not token and existing:
        token = existing.get("token", "")  # same leave-blank-to-keep rule as the API key

    if not token and not chat_id:
        return None

    errors: list[str] = []
    if not token:
        errors.append(_("Bot token is required."))
    if not chat_id:
        errors.append(_("Chat ID is required."))
    if errors:
        raise ValidationError(errors)
    return {"token": token, "chat_id": chat_id}


def schedules(form: dict, job_names) -> dict:
    """Validate the schedule for each job. Fields are prefixed with the job name.

    A schedule that cannot be parsed would leave the job silently never firing, which is the
    failure this whole feature exists to prevent — so it is rejected at the form rather than
    written and discovered later.
    """
    from dataclasses import replace
    from datetime import datetime

    from .schedule import DAYS, INTERVAL, KINDS, WEEKLY, Schedule, ScheduleError, next_run

    errors: list[str] = []
    result: dict = {}

    for name in job_names:
        kind = (form.get(f"{name}_kind") or "daily").strip()
        if kind not in KINDS:
            errors.append(_("{job}: schedule type must be one of {options}.", job=name, options=", ".join(KINDS)))
            continue

        spec = Schedule(
            enabled=form.get(f"{name}_enabled") in ("on", "true", "1"),
            kind=kind,
            at=(form.get(f"{name}_at") or "09:00").strip(),
            day=(form.get(f"{name}_day") or "monday").strip().lower(),
            every_hours=_int_or(form.get(f"{name}_every_hours"), 6),
        )

        if kind == INTERVAL and spec.every_hours < 1:
            errors.append(_("{job}: interval must be at least one hour.", job=name))
            continue
        if kind == WEEKLY and spec.day not in DAYS:
            errors.append(_("{job}: unknown day {day!r}.", job=name, day=spec.day))
            continue

        try:
            # Proving it resolves is the point: a time like "25:00" is only a problem at the
            # moment the loop tries to use it, which is hours after the operator left the page.
            # Checked as if enabled, because `next_run` short-circuits on a disabled schedule —
            # otherwise a bad time could be saved now and only break when someone enables it.
            next_run(replace(spec, enabled=True), datetime.now())
        except ScheduleError as e:
            errors.append(f"{name}: {e}")
            continue

        result[name] = spec.to_dict()

    if errors:
        raise ValidationError(errors)
    return result


def _int_or(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
