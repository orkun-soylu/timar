"""Which operating system — or which appliance — a server is, for the logo before its name.

Learned on connections timar makes anyway (enrolment, the log sweep, the update run) and never on
the status probe, which is a bare TCP connect every ten seconds and must stay one. A machine
timar has not logged in to yet shows its platform's logo, and Linux's when the platform says
nothing more.

What a machine *does* beats what it runs, where it is recognisable: a Proxmox host and a Proxmox
Datacenter Manager both report `ID=debian`, and an OctoPi board reports Raspberry Pi OS, yet the
operator knows each of them by the first.

Kept in its own file, beside `ssh/enrolled` and keyed the same way — by address — so a rename
does not lose it and it is not part of `state.json`, which the scheduler rewrites constantly.
"""
from __future__ import annotations

import json
import logging
import threading

from . import config
from .membership import address_key
from .ssh import run

logger = logging.getLogger(__name__)

OS_FILE = "ssh/os.json"

# Slug → name, for each symbol in `web/static/logos.svg` (Simple Icons, CC0).
LOGOS = {
    "debian": "Debian", "ubuntu": "Ubuntu", "kalilinux": "Kali Linux", "proxmox": "Proxmox",
    "openwrt": "OpenWrt", "raspberrypi": "Raspberry Pi", "octoprint": "OctoPrint",
    "linux": "Linux", "alpinelinux": "Alpine Linux", "archlinux": "Arch Linux",
    "fedora": "Fedora", "rockylinux": "Rocky Linux", "almalinux": "AlmaLinux",
    "opensuse": "openSUSE", "nixos": "NixOS", "linuxmint": "Linux Mint",
}

# `os-release` IDs whose logo is named differently. Everything else is looked up as itself.
_BY_ID = {
    "kali": "kalilinux", "raspbian": "raspberrypi", "alpine": "alpinelinux", "arch": "archlinux",
    "rocky": "rockylinux", "opensuse-leap": "opensuse", "opensuse-tumbleweed": "opensuse",
    "opensuse": "opensuse", "sles": "opensuse",
}

# The appliances first, then the OS. Every test is POSIX `sh` with `[ -e ]`, so busybox on
# OpenWrt runs it unchanged. The markers were read off real machines: PDM 1.1 installs
# `proxmox-datacenter-manager-admin` and Proxmox VE `pveversion`, both on a host whose
# os-release says Debian; OctoPi writes `/etc/octopi_version` (OctoPrint's own Pi Support
# plugin reads it); Raspberry Pi OS leaves `/etc/rpi-issue` even where its ID is plain `debian`.
#
# A second line names the version for the servers table: `label:` is a whole name (an appliance
# says its own), `version:` is a bare VERSION_ID that `parse` puts after the logo's name, so
# "Debian 13" rather than os-release's "Debian GNU/Linux 13 (trixie)".
DETECT = (
    "if [ -e /usr/sbin/proxmox-datacenter-manager-admin ]; then echo proxmox; "
    "echo \"label:Datacenter Manager $(dpkg-query -W -f='${Version}' proxmox-datacenter-manager 2>/dev/null)\"; "
    "elif [ -e /usr/bin/pveversion ]; then echo proxmox; "
    "echo \"label:Proxmox VE $(pveversion 2>/dev/null | cut -d/ -f2)\"; "
    "elif [ -e /etc/octopi_version ]; then echo octoprint; echo \"label:OctoPi $(cat /etc/octopi_version)\"; "
    "elif [ -e /etc/rpi-issue ]; then echo raspberrypi; . /etc/os-release; "
    "echo \"label:Raspberry Pi OS $VERSION_ID\"; "
    "elif [ -r /etc/os-release ]; then . /etc/os-release; echo \"$ID $ID_LIKE\"; echo \"version:$VERSION_ID\"; fi"
)

_lock = threading.Lock()


def parse(output: str) -> str | None:
    """The logo for what `DETECT` printed: its ID, else the first of its ID_LIKE we have."""
    first = output.strip().splitlines()[0] if output.strip() else ""
    for word in first.split():
        word = word.strip().strip('"').lower()
        slug = _BY_ID.get(word, word)
        if slug in LOGOS:
            return slug
    return None


def parse_version(output: str, slug: str | None) -> str | None:
    """The short system name for the servers table — "Debian 13", "Proxmox VE 9.2.20" — or None."""
    for line in output.strip("\n").splitlines()[1:]:
        kind, _, raw = line.partition(":")
        kind, value = kind.strip(), raw.strip().strip('"')
        if kind == "label":
            # "Proxmox VE " — the version part came back empty: half a name is worse than none.
            return value if value and not raw.endswith(" ") else None
        if kind == "version" and slug:
            return f"{LOGOS[slug]} {value}" if value else LOGOS[slug]
    return None


def _load() -> dict[str, dict]:
    path = config.path(OS_FILE)
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}     # a logo is decoration; a damaged file costs it, nothing else
    # 0.2.21 – 0.2.24 stored the slug alone.
    return {k: (v if isinstance(v, dict) else {"logo": v}) for k, v in raw.items()}


def seen() -> dict[str, str]:
    """Address → logo slug, for every machine whose OS has been read."""
    with _lock:
        return {k: v["logo"] for k, v in _load().items() if v.get("logo")}


def versions() -> dict[str, str]:
    """Address → system name ("Debian 13"), where one has been read."""
    with _lock:
        return {k: v["version"] for k, v in _load().items() if v.get("version")}


def note(ssh, address: str) -> None:
    """Read the OS over an open connection and remember it. Never raises: a logo is never
    worth failing a sweep or an update over."""
    try:
        out, _, _ = run(ssh, DETECT, timeout=15)
        slug = parse(out)
        if not slug:
            return
        entry = {"logo": slug}
        if version := parse_version(out, slug):
            entry["version"] = version
        key = address_key(address)
        with _lock:
            known = _load()
            if known.get(key) == entry:
                return
            known[key] = entry
            config.path(OS_FILE).parent.mkdir(parents=True, exist_ok=True)
            config.write_private(OS_FILE, json.dumps(known, indent=2, sort_keys=True))
    except Exception as e:
        logger.debug("could not read the OS of %s: %s", address, e)


def system(server: dict, known: dict[str, str]) -> str:
    """The servers table's *System* cell: what was read, else the platform."""
    return known.get(address_key(server["host"])) or server.get("platform", "linux")


def logo(server: dict, known: dict[str, str] | None = None) -> str:
    """The slug drawn before a server's name."""
    platform = server.get("platform", "linux")
    if platform == "proxmox":
        return "proxmox"    # its os-release says Debian; the platform says what it is
    if slug := (known if known is not None else seen()).get(address_key(server["host"])):
        return slug
    return "openwrt" if platform == "openwrt" else "linux"
