"""SSH connections, with host keys pinned on first sight.

Trust-on-first-use against `/data/ssh/known_hosts`: the first connection to a host is taken on
faith and recorded, and every connection after it is checked against that record. A changed key
raises `BadHostKeyException` rather than connecting.

This replaces a bare `AutoAddPolicy` with no `known_hosts` file, which accepted any key from any
host on every connection and wrote nothing down. That is not weaker protection than TOFU — it is
none, and it looked identical from the outside.
"""
import logging
from contextlib import contextmanager

import paramiko

from . import config
from .network import split_address

KNOWN_HOSTS = "ssh/known_hosts"

logger = logging.getLogger(__name__)


class TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Accept a host never seen before, and write its key down so it is checked from then on.

    Only reached for a host with *no* entry in `known_hosts`: paramiko checks a known host itself
    and raises `BadHostKeyException` on a changed key before any policy is asked. Written out
    rather than borrowed from `AutoAddPolicy`, which does the same two steps but reads — to a
    reviewer and to static analysis alike — as "accept anything", and says nothing about the
    save that makes the first sight the only blind one.
    """

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(str(config.path(KNOWN_HOSTS)))
        logger.info("pinned the %s host key of %s on first sight", key.get_name(), hostname)


def new_client() -> paramiko.SSHClient:
    """An SSH client checking against Timar's `known_hosts`, pinning a new host on first sight.

    Every connection Timar makes — key or password, enrolment or update — starts here, so there
    is one trust rule and not one per caller.
    """
    client = paramiko.SSHClient()
    path = config.path(KNOWN_HOSTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    client.load_host_keys(str(path))
    client.set_missing_host_key_policy(TrustOnFirstUse())
    return client


@contextmanager
def connect(host, user, ssh_key, port=None, timeout=30):
    """`host` may carry its port (`10.0.0.5:2222`); see `network.split_address`."""
    hostname, own_port = split_address(host)
    client = new_client()
    client.connect(
        hostname=hostname,
        port=port or own_port,
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
