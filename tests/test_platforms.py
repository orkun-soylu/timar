"""Parser tests built from output captured on real hosts.

The fixtures below are verbatim `df -hP` output from an OpenWrt 25.12 router (busybox 1.37) and
a Debian 13 machine. Hand-written fixtures would have hidden the two differences that matter:
the fifth column is headed "Capacity" on busybox and "Use%" on coreutils, and OpenWrt's /rom is
always 100% full.
"""
import pytest

from timar.platforms import OpenWrt, Platform, Proxmox, get

# Captured: ssh root@<openwrt> 'df -hP'
OPENWRT_DF = """\
Filesystem                Size      Used Available Capacity Mounted on
/dev/root                 5.3M      5.3M         0 100% /rom
tmpfs                     1.9G     15.3M      1.9G   1% /tmp
/dev/loop0               87.0M    678.0K     79.5M   1% /overlay
overlayfs:/overlay       87.0M    678.0K     79.5M   1% /
tmpfs                   512.0K         0    512.0K   0% /dev
"""

# Captured: df -hP on Debian 13
DEBIAN_DF = """\
Filesystem                    Size  Used Avail Use% Mounted on
/dev/nvme0n1p2                917G  147G  733G  17% /
/dev/nvme0n1p1                510M   67M  444M  14% /boot/firmware
192.0.2.10:/volume1/backups   1.8T  386G  1.4T  22% /mnt/nas-backup
"""


class TestParseDisk:
    def test_openwrt_rom_is_not_an_issue(self):
        """/rom is a read-only squashfs and reads 100% on every OpenWrt device, forever.

        Without the exclusion this fires a critical alert on every run of every router, which
        trains the operator to ignore the report.
        """
        assert OpenWrt().parse_disk(OPENWRT_DF, threshold=85) == []

    def test_openwrt_rom_would_otherwise_trip(self):
        # Guards the exclusion itself: drop skip_mounts and this test starts failing.
        plain = Platform()
        assert "/rom: 100%" in plain.parse_disk(OPENWRT_DF, threshold=85)

    def test_debian_below_threshold(self):
        assert Platform().parse_disk(DEBIAN_DF, threshold=85) == []

    def test_debian_reports_mounts_at_or_above_threshold(self):
        # `/` is exactly at the threshold and must be included; /boot/firmware at 14% must not.
        assert Platform().parse_disk(DEBIAN_DF, threshold=17) == [
            "/: 17%",
            "/mnt/nas-backup: 22%",
        ]

    def test_nfs_device_names_are_not_mistaken_for_pseudo_filesystems(self):
        # The device column here is "192.0.2.10:/volume1/backups" — it must be read as a
        # device, not split into extra fields that shift the percentage column.
        assert "/mnt/nas-backup: 22%" in Platform().parse_disk(DEBIAN_DF, threshold=20)

    def test_ram_backed_mounts_are_skipped(self):
        full_tmpfs = (
            "Filesystem Size Used Available Capacity Mounted on\n"
            "tmpfs 1.9G 1.9G 0 100% /tmp\n"
        )
        assert OpenWrt().parse_disk(full_tmpfs, threshold=85) == []

    def test_header_is_dropped_on_both_column_namings(self):
        # "Capacity" and "Use%" both live in the header row; neither may become a finding.
        for fixture in (OPENWRT_DF, DEBIAN_DF):
            assert not any("Mounted" in row for row in Platform().parse_disk(fixture, 0))

    def test_short_rows_are_ignored(self):
        assert Platform().parse_disk("Filesystem Size\ngarbage\n", threshold=0) == []


class TestCommands:
    def test_openwrt_does_not_use_journalctl(self):
        """busybox has no journalctl; asking for it returns nothing and looks like good news."""
        assert "journalctl" not in OpenWrt().journal_cmd(6)
        assert "logread" in OpenWrt().journal_cmd(6)

    def test_disk_cmd_avoids_the_gnu_only_flag(self):
        # `--output=pcent,target` is rejected by busybox with a non-zero exit.
        for p in (Platform(), OpenWrt(), Proxmox()):
            assert "--output" not in p.disk_cmd()

    def test_openwrt_has_no_default_update_command(self):
        """Updating a router unattended can strand the session used to repair it."""
        assert OpenWrt().default_update_cmd is None
        assert Platform().default_update_cmd is not None

    def test_openwrt_shutdown_never_calls_sudo(self):
        assert OpenWrt().shutdown_cmd("root") == "poweroff"
        assert "sudo" not in OpenWrt().shutdown_cmd("someone")

    def test_linux_shutdown_uses_sudo_for_non_root(self):
        assert Platform().shutdown_cmd("root") == "shutdown -h now"
        assert Platform().shutdown_cmd("deploy").startswith("sudo ")

    def test_platforms_without_docker_return_no_command(self):
        assert OpenWrt().docker_cmd() is None
        assert Proxmox().docker_cmd() is None
        assert Platform().docker_cmd() is not None


class TestResolution:
    @pytest.mark.parametrize("pid,expected", [
        ("linux", "linux"), ("openwrt", "openwrt"), ("proxmox", "proxmox"),
    ])
    def test_known_ids(self, pid, expected):
        assert get(pid).id == expected

    def test_unknown_and_missing_fall_back_to_linux(self):
        """A typo in one server's config must not take down the run for the others."""
        assert get("opnewrt").id == "linux"
        assert get(None).id == "linux"
