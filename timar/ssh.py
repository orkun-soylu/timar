import paramiko
from contextlib import contextmanager


@contextmanager
def connect(host, user, ssh_key, port=22, timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=user,
        key_filename=ssh_key,
        timeout=timeout,
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
