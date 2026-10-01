"""An address may carry its SSH port, and every connection made from it has to use that port."""
import socket

import pytest

from timar import network


@pytest.mark.parametrize("address, expected", [
    ("10.0.0.5", ("10.0.0.5", 22)),
    ("host.lan", ("host.lan", 22)),
    ("10.0.0.5:2222", ("10.0.0.5", 2222)),
    ("host.lan:2345", ("host.lan", 2345)),
    ("fd00::5", ("fd00::5", 22)),                 # a bare IPv6 address's colons are not a port
    ("[fd00::5]:2222", ("fd00::5", 2222)),
    ("[fd00::5]", ("fd00::5", 22)),
])
def test_split_address(address, expected):
    assert network.split_address(address) == expected


def test_the_probe_knocks_on_the_port_in_the_address():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert network.is_host_up(f"127.0.0.1:{port}", timeout=1)
    # Closed now: the same address no longer answers.
    assert not network.is_host_up(f"127.0.0.1:{port}", timeout=1)


def test_addresses_sort_by_ip_then_port():
    addresses = ["10.0.0.10", "10.0.0.9:2222", "10.0.0.9", "a.lan"]
    assert sorted(addresses, key=network.address_key) == ["10.0.0.9", "10.0.0.9:2222",
                                                          "10.0.0.10", "a.lan"]


def test_connect_uses_the_port_in_the_address(monkeypatch, tmp_path):
    import paramiko

    from timar import ssh
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    seen = {}
    monkeypatch.setattr(paramiko.SSHClient, "connect",
                        lambda self, **kw: seen.update(kw))
    with ssh.connect("127.0.0.1:2345", "op", None):
        pass
    assert (seen["hostname"], seen["port"]) == ("127.0.0.1", 2345)
