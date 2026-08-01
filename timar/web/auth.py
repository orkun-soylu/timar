"""One operator, one password, one signed cookie.

Timar holds an SSH key that reaches every machine it manages and can write `sudoers` on them.
Its blast radius is the whole fleet, so "it is only on the LAN" is not an access-control story —
site-to-site links, a guest VLAN, and a forwarded port all end at the same login form.

Single-user is a deliberate scope, not a shortcut: there are no roles to divide when every
capability is administrative. What that removes is user management, not authentication.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import HTTPException, Request, status

from .. import config

ALGORITHM = "HS256"
SESSION_COOKIE = "timar_session"
SESSION_DAYS = 30

# A local brute-force guard: slow enough to make guessing hopeless, short enough that an
# operator who fat-fingers their own password is not locked out for the evening.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


class AuthError(RuntimeError):
    pass


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


_failures: dict[str, _Attempts] = {}


def secret_key() -> str:
    """The cookie signing key, generated into the volume on first use.

    Regenerating it invalidates every existing session — which is also how you log out a stolen
    cookie: delete the file and restart.
    """
    p = config.path(config.SECRET_KEY)
    if p.exists():
        return p.read_text().strip()
    key = secrets.token_urlsafe(48)
    config.write_private(config.SECRET_KEY, key)
    return key


def account() -> dict | None:
    p = config.path(config.AUTH)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def create_account(username: str, password: str) -> None:
    """First-run only. Refuses to overwrite an existing account.

    The guard matters because the setup route is reachable without a session — it has to be,
    there is no account yet. Without this check that route stays a password reset for anyone who
    can load the page.
    """
    if account() is not None:
        raise AuthError("an operator account already exists")
    username = username.strip()
    if not username:
        raise AuthError("username is required")
    if len(password) < 12:
        # Long rather than ornate: this password guards SSH access to every managed host, and a
        # composition rule ("one symbol, one digit") buys far less than length.
        raise AuthError("password must be at least 12 characters")

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    config.write_private(config.AUTH, json.dumps({"username": username, "password_hash": hashed}))


def _locked_for(username: str) -> int:
    record = _failures.get(username)
    if record and record.locked_until > time.monotonic():
        return int(record.locked_until - time.monotonic())
    return 0


def verify(username: str, password: str) -> str:
    """Return the username on success; raise AuthError otherwise.

    The failure message never distinguishes "no such user" from "wrong password" — the
    difference tells an attacker which half to keep working on.
    """
    if remaining := _locked_for(username):
        raise AuthError(f"too many attempts, try again in {remaining}s")

    acct = account()
    ok = (
        acct is not None
        and secrets.compare_digest(acct["username"], username)
        and bcrypt.checkpw(password.encode(), acct["password_hash"].encode())
    )
    if not ok:
        record = _failures.setdefault(username, _Attempts())
        record.count += 1
        if record.count >= MAX_ATTEMPTS:
            record.locked_until = time.monotonic() + LOCKOUT_SECONDS
            record.count = 0
        raise AuthError("incorrect username or password")

    _failures.pop(username, None)
    return username


def issue_token(username: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + SESSION_DAYS * 86400},
        secret_key(),
        algorithm=ALGORITHM,
    )


def read_token(token: str | None) -> str | None:
    """The username a cookie proves, or None. Never raises — a bad cookie is just not a session."""
    if not token:
        return None
    try:
        claims = jwt.decode(token, secret_key(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None

    username = claims.get("sub")
    acct = account()
    # The account can be recreated on a restored volume; a cookie signed for a username that is
    # no longer the operator must not still open the door.
    if acct is None or username != acct["username"]:
        return None
    return username


def require_operator(request: Request) -> str:
    """FastAPI dependency: the signed-in operator, or a 401 that the app turns into /login.

    Lives here rather than in `app.py` so every router can depend on it without importing the
    application — a settings router that cannot reach the guard is a settings router that ends
    up unguarded.
    """
    username = read_token(request.cookies.get(SESSION_COOKIE))
    if username is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    return username
