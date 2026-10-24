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
from ..i18n import gettext as _

ALGORITHM = "HS256"
SESSION_COOKIE = "timar_session"
SESSION_DAYS = 30

# A local brute-force guard: slow enough to make guessing hopeless, short enough that an
# operator who fat-fingers their own password is not locked out for the evening.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
# Past this many records the stale ones are dropped, then the oldest: the table is fed by
# requests nobody has authenticated, so its size cannot be up to them.
MAX_TRACKED = 1024

# bcrypt reads at most 72 bytes; bcrypt 5 raises on more instead of truncating.
_BCRYPT_MAX = 72
# Checked when the username is wrong, so a wrong name costs what a wrong password costs.
_DUMMY_HASH = bcrypt.hashpw(b"timar: no such operator", bcrypt.gensalt())
# Every name that is not the operator's shares one record per client: a name that can never
# log in has nothing to protect, and keying on it lets each request add a record.
_UNKNOWN = "\0unknown"


class AuthError(RuntimeError):
    pass


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0
    last: float = 0.0


_failures: dict[tuple[str, str], _Attempts] = {}


def secret_key() -> str:
    """The cookie signing key, generated into the volume on first use.

    Regenerating it invalidates every existing session, though logging out now does the same
    (`revoke_sessions`).
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
        raise AuthError(_("an operator account already exists"))
    username = username.strip()
    if not username:
        raise AuthError(_("username is required"))
    if len(password) < 12:
        # Long rather than ornate: this password guards SSH access to every managed host, and a
        # composition rule ("one symbol, one digit") buys far less than length.
        raise AuthError(_("password must be at least 12 characters"))

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    config.write_private(config.AUTH, json.dumps({"username": username, "password_hash": hashed}))


def _locked_for(key: tuple[str, str]) -> int:
    record = _failures.get(key)
    if record and record.locked_until > time.monotonic():
        return int(record.locked_until - time.monotonic())
    return 0


def _password_matches(password: str, hashed: bytes) -> bool:
    """One bcrypt check, whatever the input — never a shortcut, never an exception."""
    pw = password.encode()
    if len(pw) > _BCRYPT_MAX:
        bcrypt.checkpw(b"", hashed)  # the same cost as a real check; the answer is no anyway
        return False
    return bcrypt.checkpw(pw, hashed)


def _record_failure(key: tuple[str, str]) -> None:
    now = time.monotonic()
    if len(_failures) >= MAX_TRACKED:
        for k in [k for k, r in _failures.items()
                  if r.locked_until <= now and now - r.last > LOCKOUT_SECONDS]:
            del _failures[k]
        while len(_failures) >= MAX_TRACKED:
            del _failures[min(_failures, key=lambda k: _failures[k].last)]
    record = _failures.setdefault(key, _Attempts())
    if now - record.last > LOCKOUT_SECONDS:
        record.count = 0  # failures count within a lockout window, not forever
    record.last = now
    record.count += 1
    if record.count >= MAX_ATTEMPTS:
        record.locked_until = now + LOCKOUT_SECONDS
        record.count = 0


def verify(username: str, password: str, client: str = "") -> str:
    """Return the username on success; raise AuthError otherwise.

    The failure message never distinguishes "no such user" from "wrong password" — the
    difference tells an attacker which half to keep working on. Nor does the time it takes or
    the status it ends in: a wrong name is checked against a dummy hash, and a password bcrypt
    cannot read is an ordinary wrong password.

    The lockout is per client (`client`, the peer address) as well as per account. On the
    account alone, five wrong guesses from anyone lock the operator out of their own login, as
    often as someone cares to send them.
    """
    acct = account()
    known = acct is not None and secrets.compare_digest(
        acct["username"].encode(), username.encode()
    )
    key = (client, acct["username"] if known else _UNKNOWN)
    if remaining := _locked_for(key):
        raise AuthError(_("too many attempts, try again in {seconds}s", seconds=remaining))

    matches = _password_matches(password, acct["password_hash"].encode() if known else _DUMMY_HASH)
    if not (known and matches):
        _record_failure(key)
        raise AuthError(_("incorrect username or password"))

    _failures.pop(key, None)
    return username


def issue_token(username: str) -> str:
    now = int(time.time())
    epoch = (account() or {}).get("epoch", 0)
    return jwt.encode(
        {"sub": username, "ep": epoch, "iat": now, "exp": now + SESSION_DAYS * 86400},
        secret_key(),
        algorithm=ALGORITHM,
    )


def revoke_sessions() -> None:
    """Every cookie issued so far stops working: logging out ends the session, not just the
    copy of it in this browser.

    A signed cookie carries its own validity, so the server keeps one number it can change — the
    epoch in `auth.json` — and a cookie is only good for the epoch it was issued under. One
    operator means this logs out every browser, which is also what "log out" should mean for a
    cookie that may have been copied.
    """
    acct = account()
    if acct is None:
        return
    acct["epoch"] = acct.get("epoch", 0) + 1
    config.write_private(config.AUTH, json.dumps(acct))


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
    # Cookies from before epochs existed carry none; they belong to epoch 0, so an upgrade does
    # not log anyone out, and the first logout after it does.
    if claims.get("ep", 0) != acct.get("epoch", 0):
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
