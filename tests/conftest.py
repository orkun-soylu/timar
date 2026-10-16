"""Tests never reach the network.

The fixtures use made-up hosts (10.0.0.x) that are real addresses on someone's LAN — on the
maintainer's, a sensor flagged the suite's SSH attempts as an outbound scan. Any connect to a
non-loopback address is refused and fails the test that made it; mock the probe instead.
"""

import socket

import pytest

_real_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(address[:2])
            raise ConnectionRefusedError(f"tests may not reach the network: {address[:2]}")
        return _real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    yield
    assert not attempts, f"test tried to reach the network: {attempts}"
