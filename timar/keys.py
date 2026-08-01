"""The key this installation presents to every machine it manages.

Generated in-process with `cryptography` rather than by shelling out to `ssh-keygen`, because
the container has no OpenSSH client and adding one to carry a single keypair is a poor trade.

Generated **once, lazily, into the volume** — so it survives upgrades and image rebuilds. If it
were regenerated on start, every managed host would need re-enrolling after each restart, which
is the kind of thing that gets solved by disabling key checking.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import config

COMMENT = "timar"


def exists() -> bool:
    return config.path(config.SSH_KEY).exists()


def ensure() -> str:
    """Path to the private key, generating the pair on first use."""
    private_path = config.path(config.SSH_KEY)
    if private_path.exists():
        return str(private_path)

    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()

    # 0600 before the file is visible, same as every other secret in /data.
    config.write_private(config.SSH_KEY, private)
    config.write_private(f"{config.SSH_KEY}.pub", f"{public} {COMMENT}\n")
    return str(private_path)


def public_key() -> str:
    """The single authorized_keys line, generating the pair if it does not exist yet."""
    ensure()
    return config.path(f"{config.SSH_KEY}.pub").read_text().strip()


def fingerprint() -> str:
    """`SHA256:...`, the form ssh-keygen prints, so it can be compared by eye."""
    parts = public_key().split()
    if len(parts) < 2:
        return ""
    digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
