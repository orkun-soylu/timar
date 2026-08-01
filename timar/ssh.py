"""SSH connections, with host keys pinned on first sight.

Trust-on-first-use against `/data/ssh/known_hosts`: the first connection to a host is taken on
faith and recorded, and every connection after it is checked against that record. A changed key
raises `BadHostKeyException` rather than connecting.

This replaces a bare `AutoAddPolicy` with no `known_hosts` file, which accepted any key from any
host on every connection and wrote nothing down. That is not weaker protection than TOFU — it is
none, and it looked identical from the outside.
"""
from contextlib import contextmanager

import paramiko

from . import config

KNOWN_HOSTS = "ssh/known_hosts"


def _client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    path = config.path(KNOWN_HOSTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    # load_host_keys also records the filename, which is what makes AutoAddPolicy persist a
    # newly seen key instead of forgetting it the moment the process exits.
    client.load_host_keys(str(path))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


@contextmanager
def connect(host, user, ssh_key, port=22, timeout=30):
    client = _client()
    client.connect(
        hostname=host,
        port=port,
        username=user,
        key_filename=ssh_key,
        timeout=timeout,
        allow_agent=False,     # nothing on this container's side should supply a key but us
        look_for_keys=False,
    )
    try:
        yield client
    finally:
        client.close()


def run(client, command, timeout=120):
    """Returns (stdout, stderr, exit_code)."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return stdout.read().decode(), stderr.read().decode(), exit_code
