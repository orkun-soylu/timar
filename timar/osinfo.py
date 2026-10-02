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

# Slug → name, for each symbol in `web/static/os.svg` (Simple Icons, CC0).
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
DETECT = (
    "if [ -e /usr/sbin/proxmox-datacenter-manager-admin ] || [ -e /usr/bin/pveversion ]; "
    "then echo proxmox; "
    "elif [ -e /etc/octopi_version ]; then echo octoprint; "
    "elif [ -e /etc/rpi-issue ]; then echo raspberrypi; "
    "elif [ -r /etc/os-release ]; then . /etc/os-release; echo \"$ID $ID_LIKE\"; fi"
)

_lock = threading.Lock()


def parse(output: str) -> str | None:
    """The logo for what `DETECT` printed: its ID, else the first of its ID_LIKE we have."""
    for word in output.split():
        word = word.strip().strip('"').lower()
        slug = _BY_ID.get(word, word)
        if slug in LOGOS:
            return slug
    return None


def _load() -> dict[str, str]:
    path = config.path(OS_FILE)
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}     # a logo is decoration; a damaged file costs it, nothing else


def seen() -> dict[str, str]:
    """Address → logo slug, for every machine whose OS has been read."""
    with _lock:
        return _load()


def note(ssh, address: str) -> None:
    """Read the OS over an open connection and remember it. Never raises: a logo is never
    worth failing a sweep or an update over."""
    try:
        out, _, _ = run(ssh, DETECT, timeout=15)
        slug = parse(out)
        if not slug:
            return
        key = address_key(address)
        with _lock:
            known = _load()
            if known.get(key) == slug:
                return
            known[key] = slug
            config.path(OS_FILE).parent.mkdir(parents=True, exist_ok=True)
            config.write_private(OS_FILE, json.dumps(known, indent=2, sort_keys=True))
    except Exception as e:
        logger.debug("could not read the OS of %s: %s", address, e)


def logo(server: dict, known: dict[str, str] | None = None) -> str:
    """The slug drawn before a server's name."""
    platform = server.get("platform", "linux")
    if platform == "proxmox":
        return "proxmox"    # its os-release says Debian; the platform says what it is
    if slug := (known if known is not None else seen()).get(address_key(server["host"])):
        return slug
    return "openwrt" if platform == "openwrt" else "linux"
