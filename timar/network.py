import ipaddress
import socket
import time

SSH_PORT = 22


def split_address(address: str) -> tuple[str, int]:
    """`host`, `host:port` or `[v6]:port` → (host, port); SSH's own port when none is given.

    The port lives in the address rather than in a field of its own so that every place that
    connects — the probe, the updater, enrolment, a relay — gets it from the one string it was
    already handed. A bare IPv6 address has colons of its own, so a port after one needs the
    brackets, as in a URL.
    """
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else SSH_PORT
    if address.count(":") == 1:
        host, port = address.split(":")
        return host, int(port)
    return address, SSH_PORT


def address_key(address: str):
    """IPs in numeric order — 10.0.0.9 before 10.0.0.10 — and names after them, alphabetically."""
    host, port = split_address(address)
    try:
        ip = ipaddress.ip_address(host)
        return (0, ip.version, int(ip), port, "")
    except ValueError:
        return (1, 0, 0, port, host.casefold())


def is_host_up(host: str, port: int | None = None, timeout: float = 3.0) -> bool:
    name, own_port = split_address(host)
    try:
        with socket.create_connection((name, port or own_port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def wait_for_host(host: str, port: int | None = None, max_wait: int = 180, interval: int = 10) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if is_host_up(host, port):
            time.sleep(5)  # SSH is up but sshd may still be initializing
            return True
        time.sleep(interval)
    return False
