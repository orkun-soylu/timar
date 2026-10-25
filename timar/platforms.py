"""Per-platform command sets.

The same question ("is any filesystem nearly full?", "what errors are in the log?") needs a
different command on OpenWrt than on Debian, and the wrong one usually fails *quietly* rather
than loudly. Every command here was run against a real host before being written down; see
ARCHITECTURE.md for the measurements.

Adding a platform means subclassing `Platform` and registering it in `PLATFORMS`.
"""
from __future__ import annotations

from . import windows


class Platform:
    """Linux with systemd — the default. Debian, Ubuntu, Raspberry Pi OS, Arch, ..."""

    id = "linux"
    label = "Linux (systemd)"

    supports_sudo = True
    supports_docker = True

    # Where this platform's SSH server reads authorised keys from. Appending to the wrong file
    # fails silently: the write succeeds, and the key simply never works.
    #
    # Relative means relative to the account's home; absolute paths start with "/". Deliberately
    # NOT written as "~/.ssh/...": the caller shell-quotes this value, and a tilde inside quotes
    # is not expanded by the shell — it produced a literal directory named `~` and an
    # authorized_keys file nothing would ever read. The command reported success. See
    # `enroll._install_key`.
    authorized_keys = ".ssh/authorized_keys"

    # Ask the web ports (`status.WEB_PORTS`) before SSH when checking whether the host is up.
    # For a platform whose SSH server logs every bare connect somewhere it hurts.
    quiet_probe = False

    # `None` means "this platform has no safe default"; the operator must supply one.
    #
    # `--with-new-pkgs`: plain `upgrade` holds back anything that needs a package it does not
    # have yet — a kernel ABI bump, a driver metapackage — so security kernels sat "kept back"
    # and were never installed, silently. Unlike `full-upgrade` it still never removes a
    # package. `--force-confold` keeps a locally edited config file rather than stopping at a
    # prompt nobody will answer. Then the usual tidy-up.
    default_update_cmd = (
        "sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold "
        "upgrade --with-new-pkgs && "
        "sudo apt-get autoremove -y && sudo apt-get clean"
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
        # JSON, not bare names: the compose working-directory label is what lets the sweep tell
        # a project that is stopped on purpose from one that fell over.
        return "docker ps -a --filter status=exited --no-trunc --format json 2>/dev/null"

    # -- power ---------------------------------------------------------------

    def shutdown_cmd(self, user: str) -> str:
        return "shutdown -h now" if user == "root" else "sudo shutdown -h now"

    def restart_cmd(self, user: str) -> str | None:
        """None: timar never restarts a machine of this platform. See `restart_needed_cmd`."""
        return None

    # -- the update run ------------------------------------------------------

    # Shown as the platform default in the server form, where the command itself would not be
    # readable. None shows the command.
    default_update_label: str | None = None

    # Prints a reason when the machine is in use and must not be updated now — empty when it is
    # free. Asked before the update command; a busy machine is skipped, not failed.
    busy_cmd: str | None = None

    # Prints `restart` when the update left a restart pending and nobody would lose a session to
    # it, `signed-in` when it is pending but someone is signed in, nothing otherwise. Linux has
    # none: timar does not restart Linux hosts, and a kernel that waits for the next boot is
    # how they are run.
    restart_needed_cmd: str | None = None

    # Asked over SSH by `osinfo`; None means the POSIX detection there.
    detect_cmd: str | None = None

    # The account timar is logged in as, for the enrolment check.
    whoami_cmd = "id -un"

    # False where timar cannot append its own key over a password login; the README says how to
    # place it instead.
    password_enrol = True


class OpenWrt(Platform):
    """OpenWrt / busybox. No systemd, no sudo, no package manager we should drive by default."""

    id = "openwrt"
    label = "OpenWrt"

    supports_sudo = False       # everything already runs as root
    supports_docker = False

    # dropbear, not OpenSSH: it reads this path and ignores ~/.ssh/authorized_keys entirely.
    # A key appended to the usual location on a router is written successfully and never used.
    authorized_keys = "/etc/dropbear/authorized_keys"

    # dropbear writes two syslog lines for every bare TCP connect, and a router's syslog is a
    # ring buffer in RAM. Measured on two routers probed every ten seconds: 91% and 100% of the
    # buffer was the probe, so the log sweep read little else. uhttpd answers on 80/443 and
    # logs nothing; SSH is asked only if neither answers.
    quiet_probe = True

    # Deliberately no default. `apk upgrade` on a router can exhaust the overlay partition or
    # pull a kernel-module mismatch, and the machine that breaks is the one carrying the SSH
    # session used to fix it. Updates stay off until an operator writes a command themselves.
    default_update_cmd = None

    # /rom is the read-only squashfs image and reads 100% full on every OpenWrt device, always,
    # by design. Left in, it would fire a critical disk alert on every run of every router —
    # the fastest way to teach an operator to ignore this report.
    skip_mounts = frozenset({"/rom"})

    def journal_cmd(self, hours: int) -> str:
        # busybox logread has neither `-p` nor a time window; it prints the whole ring buffer,
        # which on a quiet router reaches back to its last boot. Unfiltered, a week-old outage
        # came back as "look at this today" in every sweep until the buffer wrapped.
        #
        # `-t` adds the entry's epoch as the sixth field ("Wed Oct 14 02:42:51 2026 [1791934971.068]
        # cron.err ..."), and the cutoff is taken from the router's own clock, so neither time
        # zones nor clock skew between the two machines move the window. A line without that
        # field is kept rather than dropped: if `-t` ever stops working, the sweep goes back to
        # reporting too much instead of silently reporting nothing.
        # Severity lives in a "facility.severity" token, so it is filtered textually.
        return (
            f"c=$(( $(date +%s) - {int(hours) * 3600} )); "
            "logread -t 2>/dev/null"
            r""" | awk -v c="$c" '$6 ~ /^\[[0-9]/ { if (substr($6, 2) + 0 < c) next; sub(/ \[[0-9.]+\]/, "") } { print }'"""
            r" | grep -E '\.(emerg|alert|crit|err)( |$|\[)' | tail -50"
        )

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


class Windows(Platform):
    """Windows 10/11 with its own OpenSSH Server, PowerShell as the login shell.

    Every command is PowerShell, sent through `windows.command` — see `windows.py` for why, and
    for what was measured.
    """

    id = "windows"
    label = "Windows"

    supports_sudo = False       # an administrator's SSH session is already elevated
    supports_docker = False

    # Windows' sshd reads an administrator's keys from this file only, never from the profile's
    # `.ssh/authorized_keys` — the same silent trap as dropbear's.
    authorized_keys = "C:/ProgramData/ssh/administrators_authorized_keys"
    password_enrol = False      # nothing POSIX to append the key with; placed by hand
    whoami_cmd = "whoami"

    default_update_cmd = windows.UPDATE
    default_update_label = ("Windows Update (software, not drivers), as SYSTEM through a task "
                            "that lasts the run")
    busy_cmd = windows.BUSY
    restart_needed_cmd = windows.RESTART_NEEDED
    detect_cmd = windows.DETECT

    def journal_cmd(self, hours: int) -> str:
        return windows.journal(hours)

    def disk_cmd(self) -> str:
        return windows.DISK

    def parse_disk(self, stdout: str, threshold: int = 85) -> list[str]:
        """`windows.DISK` prints "C 16" per fixed drive."""
        issues = []
        for line in stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[1].isdigit():
                continue
            if int(fields[1]) >= threshold:
                issues.append(f"{fields[0]}: {fields[1]}%")
        return issues

    def shutdown_cmd(self, user: str) -> str:
        # /f: an application waiting on "save changes?" would otherwise hold the machine up
        # indefinitely, with nobody there to answer it.
        return "shutdown /s /t 0 /f"

    def restart_cmd(self, user: str) -> str | None:
        return "shutdown /r /t 0 /f"


PLATFORMS: dict[str, Platform] = {
    p.id: p() for p in (Platform, OpenWrt, Proxmox, Windows)
}

DEFAULT_PLATFORM = Platform.id


def get(platform_id: str | None) -> Platform:
    """Resolve a platform id, falling back to plain Linux for unknown or missing values.

    Falling back rather than raising is deliberate: a typo in one server's config should cost
    that server the right commands, not take down the run for every other server.
    """
    return PLATFORMS.get(platform_id or DEFAULT_PLATFORM, PLATFORMS[DEFAULT_PLATFORM])
