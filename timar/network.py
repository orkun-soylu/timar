import socket
import time


def is_host_up(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def wait_for_host(host: str, port: int = 22, max_wait: int = 180, interval: int = 10) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if is_host_up(host, port):
            time.sleep(5)  # SSH is up but sshd may still be initializing
            return True
        time.sleep(interval)
    return False
