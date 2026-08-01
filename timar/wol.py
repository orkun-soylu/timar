"""Wake-on-LAN, sent directly or through another machine.

A magic packet is a broadcast, and a broadcast only reaches its own segment. That is one fact
with two consequences:

- **Container networking.** From a bridge network the send succeeds and the packet never leaves
  the bridge — measured, see ARCHITECTURE.md. Host networking fixes that.
- **Other subnets.** Host networking does *not* fix a machine in a different subnet — a second
  site reached over a tunnel, an office network. No local configuration helps, because the
  packet has to originate over there.

A **relay** answers both: send the packet from a machine already on the target's segment and
already reachable over SSH. Timar manages such machines by definition, so this costs an
always-on host in that subnet and nothing else.
"""
from __future__ import annotations

import logging
import shlex
import socket

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9


class WolError(RuntimeError):
    pass


def magic_packet(mac: str) -> bytes:
    cleaned = mac.replace(":", "").replace("-", "").strip()
    if len(cleaned) != 12:
        raise WolError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(cleaned) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = DEFAULT_PORT) -> None:
    """Send from this machine. Needs host networking to reach the LAN from a container."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet(mac), (broadcast, port))


def relay_script(mac: str, broadcast: str, port: int) -> str:
    """The Python the relay runs.

    **Values here are escaped with `repr`, not `shlex.quote`.** There are two nested languages:
    Python inside a shell command. `shlex.quote` is the shell layer's tool and returns a bare
    word for anything without shell metacharacters — so a hex MAC came through unquoted and
    landed in the Python source as an undefined identifier. The remote raised a `NameError`;
    nothing local could have noticed.

    Note `SO_BROADCAST`: without it the send fails with EACCES, and only for broadcast
    addresses — which is every real use of a relay.
    """
    hex_mac = mac.replace(":", "").replace("-", "")
    return (
        "import socket;"
        f"p=b'\\xff'*6+bytes.fromhex({hex_mac!r})*16;"
        "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);"
        f"s.sendto(p,({broadcast!r},{port}))"
    )


def _relay_command(mac: str, broadcast: str, port: int) -> str:
    """The shell command run on the relay.

    Python first, because an always-on Linux box almost always has it; `wakeonlan` for the ones
    that do not. `etherwake` is deliberately not attempted — it wants an interface name and
    root, and guessing the interface on someone else's machine is how you send the packet out of
    the wrong one and report success.
    """
    script = relay_script(mac, broadcast, port)
    return (
        f"if command -v python3 >/dev/null 2>&1; then python3 -c {shlex.quote(script)}; "
        f"elif command -v wakeonlan >/dev/null 2>&1; then "
        f"wakeonlan -i {shlex.quote(broadcast)} -p {port} {shlex.quote(mac)} >/dev/null; "
        f"else echo TIMAR_NO_TOOL >&2; exit 1; fi"
    )


def send_via_relay(relay: dict, mac: str, broadcast: str = "255.255.255.255",
                   port: int = DEFAULT_PORT) -> None:
    """Send the packet from `relay`, over SSH, using the key Timar already holds for it."""
    from .config import resolve_ssh_key
    from .ssh import connect, run

    magic_packet(mac)  # validate first, so a typo reports as a bad MAC and not an SSH failure

    try:
        with connect(relay["host"], relay["user"], resolve_ssh_key(relay), timeout=15) as ssh:
            _, err, code = run(ssh, _relay_command(mac, broadcast, port), timeout=20)
    except Exception as e:
        raise WolError(f"could not reach the relay {relay['name']}: {e}") from e

    if code != 0:
        if "TIMAR_NO_TOOL" in err:
            raise WolError(
                f"{relay['name']} has neither python3 nor wakeonlan, so it cannot send the "
                "packet. Install either one, or use a different relay."
            )
        raise WolError(f"{relay['name']} could not send the packet: {err.strip()[:200]}")

    logger.info("magic packet for %s sent via %s", mac, relay["name"])


def wake(server: dict, servers_by_name: dict | None = None) -> None:
    """Wake `server`, through its relay if it has one.

    Raises WolError with something an operator can act on. A caller treats failure as "the
    machine did not come up" — which is also what happens when the packet is sent perfectly and
    the target simply has Wake-on-LAN disabled in its firmware.
    """
    mac = server.get("wol_mac")
    if not mac:
        raise WolError(f"{server['name']} has no MAC address configured")

    broadcast = server.get("wol_broadcast") or "255.255.255.255"
    relay_name = server.get("wol_relay")

    if not relay_name:
        send_magic_packet(mac, broadcast)
        return

    relay = (servers_by_name or {}).get(relay_name)
    if relay is None:
        raise WolError(f"{server['name']} names a relay {relay_name!r} that is not configured")
    send_via_relay(relay, mac, broadcast)
