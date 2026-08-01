"""Per-platform command sets.

The same question ("is any filesystem nearly full?", "what errors are in the log?") needs a
different command on OpenWrt than on Debian, and the wrong one usually fails *quietly* rather
than loudly. Every command here was run against a real host before being written down; see
ARCHITECTURE.md for the measurements.

Adding a platform means subclassing `Platform` and registering it in `PLATFORMS`.
"""
from __future__ import annotations


class Platform:
    """Linux with systemd — the default. Debian, Ubuntu, Raspberry Pi OS, Arch, ..."""

    id = "linux"
    label = "Linux (systemd)"

    supports_sudo = True
    supports_docker = True

    # `None` means "this platform has no safe default"; the operator must supply one.
    default_update_cmd = (
        "sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y"
    )

    # Filesystems whose fullness is not a fault. RAM-backed mounts are excluded because a full
    # tmpfs is a different problem with a different fix, and reporting it as "disk full" sends
    # the operator to the wrong place.
    skip_devices = frozenset({"tmpfs", "devtmpfs", "none", "udev"})
    skip_mounts = frozenset()

    # -- logs ----------------------------------------------------------------

    def journal_cmd(self, hours: int) -> str:
        return (
            f"journalctl --since='-{hours}h' -p err -o short-monotonic "
            f"--no-pager 2>/dev/null | tail -50"
        )

    def parse_journal(self, stdout: str) -> list[str]:
        return [
            line.strip()
            for line in stdout.splitlines()
            if line.strip() and not line.startswith("--")
        ]

    # -- disk ----------------------------------------------------------------

    def disk_cmd(self) -> str:
        # `-P` (POSIX output) rather than `--output=pcent,target`: the latter is a GNU coreutils
        # extension that busybox rejects outright, and the rejection surfaces as a non-zero exit
        # that an earlier version of this check swallowed into an empty result — a disk check
        # that silently passed on every router it was pointed at.
        #
        # `-P` also guarantees one record per line. Plain `df` wraps long device names onto a
        # second line, which would shift every field in the parser below.
        return "df -hP 2>/dev/null"

    def parse_disk(self, stdout: str, threshold: int = 85) -> list[str]:
        """Rows at or above `threshold` percent, as "<mount>: 91%".

        Both coreutils and busybox emit six POSIX columns, but they disagree on the fifth
        header ("Use%" vs "Capacity"), so the columns are read from the end: the mount point is
        last and the percentage is second to last.
        """
        issues = []
        for line in stdout.splitlines()[1:]:  # drop the header
            fields = line.split()
            if len(fields) < 6:
                continue
            device, pct_raw, target = fields[0], fields[-2], fields[-1]
            if device in self.skip_devices or target in self.skip_mounts:
                continue
            try:
                pct = int(pct_raw.rstrip("%"))
            except ValueError:
                continue
            if pct >= threshold:
                issues.append(f"{target}: {pct}%")
        return issues

    # -- containers ----------------------------------------------------------

    def docker_cmd(self) -> str | None:
        if not self.supports_docker:
            return None
        return "docker ps -a --filter status=exited --format '{{.Names}}' 2>/dev/null"

    # -- power ---------------------------------------------------------------

    def shutdown_cmd(self, user: str) -> str:
        return "shutdown -h now" if user == "root" else "sudo shutdown -h now"


class OpenWrt(Platform):
    """OpenWrt / busybox. No systemd, no sudo, no package manager we should drive by default."""

    id = "openwrt"
    label = "OpenWrt"

    supports_sudo = False       # everything already runs as root
    supports_docker = False

    # Deliberately no default. `apk upgrade` on a router can exhaust the overlay partition or
    # pull a kernel-module mismatch, and the machine that breaks is the one carrying the SSH
    # session used to fix it. Updates stay off until an operator writes a command themselves.
    default_update_cmd = None

    # /rom is the read-only squashfs image and reads 100% full on every OpenWrt device, always,
    # by design. Left in, it would fire a critical disk alert on every run of every router —
    # the fastest way to teach an operator to ignore this report.
    skip_mounts = frozenset({"/rom"})

    def journal_cmd(self, hours: int) -> str:
        # busybox logread has neither `-p` nor a time window; it prints the whole ring buffer.
        # Severity lives in a "facility.severity" token, so it is filtered textually, and the
        # `hours` argument cannot be honoured — the buffer is simply whatever still fits in it.
        return r"logread 2>/dev/null | grep -E '\.(emerg|alert|crit|err)( |$|\[)' | tail -50"

    def parse_journal(self, stdout: str) -> list[str]:
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def shutdown_cmd(self, user: str) -> str:
        return "poweroff"


class Proxmox(Platform):
    """Proxmox VE — Debian underneath, plus guests this host is responsible for."""

    id = "proxmox"
    label = "Proxmox VE"

    default_update_cmd = (
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y && "
        "apt-get autoremove -y"
    )

    def docker_cmd(self) -> str | None:
        # Guests are VMs and containers under `qm`/`pct`, not Docker. Reporting "no stopped
        # containers" here would be true and useless.
        return None


PLATFORMS: dict[str, Platform] = {
    p.id: p() for p in (Platform, OpenWrt, Proxmox)
}

DEFAULT_PLATFORM = Platform.id


def get(platform_id: str | None) -> Platform:
    """Resolve a platform id, falling back to plain Linux for unknown or missing values.

    Falling back rather than raising is deliberate: a typo in one server's config should cost
    that server the right commands, not take down the run for every other server.
    """
    return PLATFORMS.get(platform_id or DEFAULT_PLATFORM, PLATFORMS[DEFAULT_PLATFORM])
