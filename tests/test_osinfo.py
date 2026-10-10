"""The logo before a server's name: what the machine is, read over connections already made.

The cases below are the real outputs of `osinfo.DETECT` on the machines this was written
against — including the two that make the rule necessary: a Proxmox VE host and a Proxmox
Datacenter Manager both report `ID=debian` in os-release.
"""
import importlib
import re
from pathlib import Path

import pytest

from timar import osinfo


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    from timar import config
    importlib.reload(config)
    return tmp_path


class TestParse:
    @pytest.mark.parametrize("printed, slug", [
        ("proxmox\n", "proxmox"),                 # Proxmox VE and PDM, both ID=debian underneath
        ("debian \n", "debian"),
        ("ubuntu debian\n", "ubuntu"),            # ID first, ID_LIKE after it
        ("openwrt lede openwrt\n", "openwrt"),    # busybox ash, same command
        ("kali debian\n", "kalilinux"),
        ("raspbian debian\n", "raspberrypi"),
        ("octoprint\n", "octoprint"),
        ("opensuse-tumbleweed suse opensuse\n", "opensuse"),
        ("pop ubuntu debian\n", "ubuntu"),        # unknown ID: the first known ID_LIKE
    ])
    def test_real_outputs(self, printed, slug):
        assert osinfo.parse(printed) == slug

    def test_nothing_recognisable_is_no_answer_rather_than_a_guess(self):
        assert osinfo.parse("gentoo\n") is None
        assert osinfo.parse("") is None


class TestLogo:
    def test_a_proxmox_host_is_proxmox_whatever_was_read(self):
        """Its os-release says Debian; the platform is the better witness."""
        server = {"name": "hv", "host": "10.0.0.5", "platform": "proxmox"}
        assert osinfo.logo(server, {"10.0.0.5": "debian"}) == "proxmox"

    def test_what_was_read_wins_over_the_platform(self):
        server = {"name": "pdm", "host": "10.0.0.9", "platform": "linux"}
        assert osinfo.logo(server, {"10.0.0.9": "proxmox"}) == "proxmox"

    def test_a_port_in_the_address_is_matched_as_known_hosts_writes_it(self):
        server = {"name": "vps", "host": "127.0.0.1:2345", "platform": "linux"}
        assert osinfo.logo(server, {"[127.0.0.1]:2345": "debian"}) == "debian"

    def test_never_connected_falls_back_to_the_platform_then_linux(self):
        assert osinfo.logo({"name": "r", "host": "10.0.0.1", "platform": "openwrt"}, {}) == "openwrt"
        assert osinfo.logo({"name": "x", "host": "10.0.0.2"}, {}) == "linux"


class TestVersion:
    @pytest.mark.parametrize("printed, slug, shown", [
        ("proxmox\nlabel:Proxmox VE 9.2.21\n", "proxmox", "Proxmox VE 9.2.21"),
        ("proxmox\nlabel:Datacenter Manager 1.1.7\n", "proxmox", "Datacenter Manager 1.1.7"),
        ("debian \nversion:13\n", "debian", "Debian 13"),
        ("ubuntu debian\nversion:26.04\n", "ubuntu", "Ubuntu 26.04"),
        ("openwrt lede openwrt\nversion:25.12.5\n", "openwrt", "OpenWrt 25.12.5"),
        ("kali debian\nversion:2026.3\n", "kalilinux", "Kali Linux 2026.3"),
        ("octoprint\nlabel:OctoPi 1.1.0\n", "octoprint", "OctoPi 1.1.0"),
        ("arch\nversion:\n", "archlinux", "Arch Linux"),            # rolling: no VERSION_ID
    ])
    def test_real_outputs(self, printed, slug, shown):
        assert osinfo.parse(printed) == slug
        assert osinfo.parse_version(printed, slug) == shown

    def test_an_appliance_whose_version_could_not_be_read_says_nothing_rather_than_half(self):
        assert osinfo.parse_version("proxmox\nlabel:Proxmox VE \n", "proxmox") is None

    def test_the_cell_is_looked_up_the_way_the_file_is_keyed(self):
        """By `known_hosts`' address form — a port in brackets — not by a sort key."""
        known = {"[127.0.0.1]:2345": "Debian 13", "10.0.0.5": "Proxmox VE 9.2.21"}
        assert osinfo.system({"host": "127.0.0.1:2345", "platform": "linux"}, known) == "Debian 13"
        assert osinfo.system({"host": "10.0.0.5", "platform": "proxmox"}, known) == "Proxmox VE 9.2.21"
        assert osinfo.system({"host": "10.0.0.9", "platform": "openwrt"}, known) == "openwrt"

    def test_the_old_file_with_slugs_alone_still_reads(self, data_dir):
        path = data_dir / osinfo.OS_FILE
        path.parent.mkdir(parents=True)
        path.write_text('{"10.0.0.5": "proxmox"}')
        assert osinfo.seen() == {"10.0.0.5": "proxmox"}
        assert osinfo.versions() == {}


class TestRemembering:
    def fake(self, monkeypatch, output):
        calls = []
        monkeypatch.setattr(osinfo, "run", lambda ssh, cmd, timeout=120: calls.append(cmd) or (output, "", 0))
        return calls

    def test_what_is_read_is_kept_by_address(self, data_dir, monkeypatch):
        self.fake(monkeypatch, "ubuntu debian\n")
        osinfo.note(object(), "10.0.0.41")
        assert osinfo.seen() == {"10.0.0.41": "ubuntu"}
        assert osinfo.versions() == {}
        assert (data_dir / osinfo.OS_FILE).stat().st_mode & 0o777 == 0o600

    def test_a_failure_to_read_costs_the_logo_and_nothing_else(self, data_dir, monkeypatch):
        """Called inside the sweep and the update: it must never raise into them."""
        def broken(*a, **kw):
            raise OSError("channel closed")
        monkeypatch.setattr(osinfo, "run", broken)
        osinfo.note(object(), "10.0.0.41")
        assert osinfo.seen() == {}

    def test_a_damaged_file_reads_as_empty(self, data_dir):
        path = data_dir / osinfo.OS_FILE
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert osinfo.seen() == {}


def test_every_logo_has_a_symbol_in_the_sprite():
    """A slug without a symbol draws nothing — an empty space where the state should be."""
    sprite = (Path(osinfo.__file__).parent / "web" / "static" / "logos.svg").read_text()
    symbols = set(re.findall(r'<symbol id="([a-z0-9]+)"', sprite))
    from timar.containers import APP_LOGOS
    assert set(osinfo.LOGOS) <= symbols
    assert set(osinfo.DEVICE_LOGOS) <= symbols
    assert set(APP_LOGOS) <= symbols


def test_the_sweep_reads_the_os_on_the_connection_it_already_has(monkeypatch):
    from timar import log_checker

    class Conn:
        def __enter__(self):
            return "ssh"

        def __exit__(self, *exc):
            return False

    noted = []
    monkeypatch.setattr(log_checker, "is_host_up", lambda host, **kw: True)
    monkeypatch.setattr(log_checker, "connect", lambda *a, **kw: Conn())
    monkeypatch.setattr(log_checker, "resolve_ssh_key", lambda s: "key")
    monkeypatch.setattr(log_checker, "run", lambda *a, **kw: ("", "", 0))
    monkeypatch.setattr(log_checker.osinfo, "note", lambda ssh, host: noted.append((ssh, host)))
    log_checker.check_server({"name": "web-01", "host": "10.0.0.7", "user": "op", "platform": "linux"})
    assert noted == [("ssh", "10.0.0.7")]

