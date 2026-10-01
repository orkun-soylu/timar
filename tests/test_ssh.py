"""Trust on first use, against a real SSH handshake.

The property that matters is the second connection, not the first: a host Timar has seen before
must present the same key, or the connection is refused.
"""
import socket
import threading

import paramiko
import pytest

from timar import ssh


class _AcceptAnyone(paramiko.ServerInterface):
    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL


def _serve_once(host_key: paramiko.PKey) -> int:
    """One SSH handshake on a free local port, presenting `host_key`."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def run():
        conn, _ = listener.accept()
        transport = paramiko.Transport(conn)
        transport.add_server_key(host_key)
        try:
            transport.start_server(server=_AcceptAnyone())
            transport.accept(timeout=5)
        except Exception:
            pass
        finally:
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def _connect(port: int) -> None:
    client = ssh.new_client()
    try:
        client.connect("127.0.0.1", port=port, username="op", password="x",
                       allow_agent=False, look_for_keys=False, timeout=5)
    finally:
        client.close()


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    (tmp_path / "ssh").mkdir()
    (tmp_path / "ssh" / "known_hosts").touch()
    return tmp_path


def test_the_policy_is_ours_not_auto_add():
    assert isinstance(ssh.new_client()._policy, ssh.TrustOnFirstUse)


def _known_hosts(data_dir) -> paramiko.HostKeys:
    return paramiko.HostKeys(str(data_dir / "ssh" / "known_hosts"))


def test_a_host_seen_for_the_first_time_is_pinned(data_dir):
    key = paramiko.RSAKey.generate(2048)
    port = _serve_once(key)
    _connect(port)
    assert _known_hosts(data_dir).lookup(f"[127.0.0.1]:{port}")["ssh-rsa"] == key


def test_a_pinned_host_presenting_its_own_key_is_accepted(data_dir):
    key = paramiko.RSAKey.generate(2048)
    port = _serve_once(key)
    _connect(port)
    port = _serve_once(key)
    pinned = _known_hosts(data_dir)
    pinned.add(f"[127.0.0.1]:{port}", "ssh-rsa", key)
    pinned.save(str(data_dir / "ssh" / "known_hosts"))
    _connect(port)                                   # no exception


def test_a_pinned_host_presenting_another_key_is_refused(data_dir):
    pinned_key, impostor = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
    port = _serve_once(impostor)
    pinned = _known_hosts(data_dir)
    pinned.add(f"[127.0.0.1]:{port}", "ssh-rsa", pinned_key)
    pinned.save(str(data_dir / "ssh" / "known_hosts"))
    with pytest.raises(paramiko.BadHostKeyException):
        _connect(port)
    # And the impostor's key was not written over the pinned one.
    assert _known_hosts(data_dir).lookup(f"[127.0.0.1]:{port}")["ssh-rsa"] == pinned_key
