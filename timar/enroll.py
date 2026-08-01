"""Enrolling a host: install Timar's key, and optionally grant it passwordless sudo.

This is the most dangerous surface in the product, for two separate reasons.

**The password.** Enrolment is the one operation that takes the operator's SSH password. It is
held in memory for the length of one request, written only to the SSH channel, and never
persisted, never logged, never echoed back to the page. Once the key is installed it is not
needed again.

**The sudoers file.** A malformed file in `/etc/sudoers.d/` does not degrade gracefully — it
breaks `sudo` for everyone on that machine, including the session you would repair it from. So
the candidate is written to a temporary file, validated with `visudo -c`, and only *then* moved
into place. That validation step is not optional and must never be optimised away.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass

import paramiko

from . import config, keys
from .platforms import get as get_platform

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 30
SUDOERS_FILE = "/etc/sudoers.d/timar"

# Sentinels, because a shell pipeline's exit code cannot distinguish "the file was rejected by
# visudo" from "the command could not run at all", and those need different messages.
OK = "TIMAR_OK"
ALREADY = "TIMAR_ALREADY"
INVALID = "TIMAR_INVALID"


class EnrollError(RuntimeError):
    pass


@dataclass
class Result:
    key_installed: bool = False
    key_already_present: bool = False
    sudo_granted: bool = False
    sudo_skipped_reason: str = ""

    def describe(self) -> str:
        parts = []
        if self.key_already_present:
            parts.append("key was already installed")
        elif self.key_installed:
            parts.append("key installed")
        if self.sudo_granted:
            parts.append("passwordless sudo granted")
        elif self.sudo_skipped_reason:
            parts.append(f"sudo not configured ({self.sudo_skipped_reason})")
        return "; ".join(parts) or "nothing to do"


def _connect(host: str, user: str, password: str, port: int = 22) -> paramiko.SSHClient:
    """Password connection for enrolment only, with host keys pinned on first sight.

    Trust-on-first-use, persisted to `/data/ssh/known_hosts`: the first connection is taken on
    faith, and every one after it is checked. The bare `AutoAddPolicy` this replaces accepted
    any key every time and never wrote anything down, which is not a weaker protection — it is
    none at all. A changed key now raises rather than connecting.
    """
    client = paramiko.SSHClient()
    known_hosts = config.path("ssh/known_hosts")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    client.load_host_keys(str(known_hosts))     # also tells paramiko where to persist new ones
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=host, port=port, username=user, password=password,
            timeout=CONNECT_TIMEOUT, allow_agent=False, look_for_keys=False,
        )
    except paramiko.BadHostKeyException as e:
        raise EnrollError(
            f"the host key for {host} has changed since Timar first connected. "
            "Either the machine was rebuilt, or something is impersonating it. "
            "Remove its entry from /data/ssh/known_hosts only if you know why it changed."
        ) from e
    except paramiko.AuthenticationException as e:
        raise EnrollError("the password was not accepted for that user") from e
    except Exception as e:
        raise EnrollError(f"could not connect to {host}: {e}") from e
    return client


def _run(client: paramiko.SSHClient, command: str, stdin_text: str | None = None) -> tuple[str, str, int]:
    stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT)
    if stdin_text is not None:
        stdin.write(stdin_text)
        stdin.flush()
        stdin.channel.shutdown_write()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err, stdout.channel.recv_exit_status()


def _remote_path(path: str) -> str:
    """Render a platform path for the remote shell, expanding home safely.

    A tilde cannot be used here. `shlex.quote` wraps the value in single quotes, and a tilde
    inside quotes is **not** expanded — the first version of this created a literal directory
    named `~`, wrote an authorized_keys file nothing would ever read, and reported success.
    `"$HOME"` survives quoting and still tolerates a space in the path.
    """
    if path.startswith("/"):
        return shlex.quote(path)
    return '"$HOME"/' + shlex.quote(path)


def _install_key(client: paramiko.SSHClient, platform, public_key: str) -> tuple[bool, bool]:
    """Append the key unless it is already there. Returns (installed, already_present)."""
    path = platform.authorized_keys
    quoted_key = shlex.quote(public_key)
    quoted_path = _remote_path(path)

    # `grep -qxF` matches the whole line literally, so a key that is already present is not
    # appended a second time -- re-enrolling a host must be safe to do.
    command = (
        f"set -e; umask 077; "
        f"d=$(dirname {quoted_path}); mkdir -p \"$d\"; "
        f"touch {quoted_path}; chmod 600 {quoted_path}; "
        f"if grep -qxF {quoted_key} {quoted_path}; then echo {ALREADY}; "
        f"else printf '%s\\n' {quoted_key} >> {quoted_path}; echo {OK}; fi"
    )
    out, err, code = _run(client, command)
    if code != 0:
        raise EnrollError(f"could not write {path}: {(err or out).strip()[:200]}")
    return OK in out, ALREADY in out


def _grant_sudo(client: paramiko.SSHClient, user: str, password: str) -> None:
    """Install a NOPASSWD rule, but only after `visudo` agrees it parses.

    Written to a temporary file, validated, then moved into place with `install` — never edited
    in place. A file that fails validation is discarded and `/etc/sudoers.d/` is left untouched,
    which is the difference between "the button did not work" and "sudo is broken on this
    machine and I cannot get back in to fix it".
    """
    rule = f"{user} ALL=(ALL) NOPASSWD:ALL"
    script = (
        f"umask 077; f=$(mktemp); "
        f"printf '%s\\n' {shlex.quote(rule)} > \"$f\"; "
        f"if visudo -cqf \"$f\"; then "
        f"  install -m 0440 -o root -g root \"$f\" {shlex.quote(SUDOERS_FILE)} && echo {OK}; "
        f"else echo {INVALID}; fi; "
        f"rm -f \"$f\""
    )
    # `-S` reads the password from stdin and `-p ''` suppresses the prompt, so the password
    # never appears in the command line -- where `ps` on the target would show it to every
    # other user on that machine.
    out, err, _ = _run(client, f"sudo -S -p '' sh -c {shlex.quote(script)}", stdin_text=password + "\n")

    if OK in out:
        return
    if INVALID in out:
        raise EnrollError("visudo rejected the generated rule; nothing was changed on the host")
    detail = (err or out).strip()[:200]
    if "incorrect password" in detail.lower() or "sorry, try again" in detail.lower():
        raise EnrollError("that password was not accepted by sudo on the host")
    raise EnrollError(f"could not configure sudo: {detail or 'no output from sudo'}")


def enroll(server: dict, password: str, *, grant_sudo: bool) -> Result:
    """Install Timar's key on a host, optionally granting it passwordless sudo."""
    platform = get_platform(server.get("platform"))
    result = Result()

    client = _connect(server["host"], server["user"], password)
    try:
        installed, already = _install_key(client, platform, keys.public_key())
        result.key_installed = installed
        result.key_already_present = already

        if not grant_sudo:
            return result
        if not platform.supports_sudo:
            # OpenWrt has no sudo and already runs everything as root; offering it would be
            # offering a button that cannot work.
            result.sudo_skipped_reason = f"{platform.label} has no sudo"
            return result
        if server["user"] == "root":
            result.sudo_skipped_reason = "the user is already root"
            return result

        _grant_sudo(client, server["user"], password)
        result.sudo_granted = True
        return result
    finally:
        client.close()


def verify(server: dict) -> str:
    """Connect with the key alone, to prove enrolment actually worked.

    Separate from `enroll` on purpose: the password connection succeeding says nothing about
    whether the *key* will be accepted, and that is the thing every later run depends on.
    """
    platform = get_platform(server.get("platform"))
    keys.ensure()  # so "no key yet" reports as an unaccepted key, not a missing-file traceback
    client = paramiko.SSHClient()
    known_hosts = config.path("ssh/known_hosts")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=server["host"], username=server["user"],
            key_filename=config.resolve_ssh_key(server),
            timeout=CONNECT_TIMEOUT, allow_agent=False, look_for_keys=False,
        )
    except paramiko.AuthenticationException as e:
        raise EnrollError("the key was not accepted — enrol the host first") from e
    except Exception as e:
        raise EnrollError(f"could not connect: {e}") from e

    try:
        out, _, _ = _run(client, "id -un")
        who = out.strip() or server["user"]
        if not platform.supports_sudo or who == "root":
            return f"connected with the key as {who}"
        _, _, code = _run(client, "sudo -n true")
        sudo = "passwordless sudo works" if code == 0 else "no passwordless sudo"
        return f"connected with the key as {who}; {sudo}"
    finally:
        client.close()
