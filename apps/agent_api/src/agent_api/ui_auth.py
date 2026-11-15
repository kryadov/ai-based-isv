"""Human login for the organization panel (M10, addendum §1 "Authorization").

Local users in v0 (OIDC later): ``AGENT_API_UI_USERS`` =
``name:sha256hex(password):role`` entries, comma-separated. Sessions are
self-contained signed cookies (HMAC-SHA256 over ``user|expiry``) — no server
session store. Agents never log in here: they are service identities.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

UI_DIR: Final[Path] = Path(__file__).resolve().parent / "ui"
COOKIE: Final[str] = "swarm_session"
SESSION_TTL_S: Final[int] = 12 * 3600


@dataclass(frozen=True)
class UiUser:
    name: str
    password_sha256: str
    role: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def parse_users(raw: str) -> dict[str, UiUser]:
    users: dict[str, UiUser] = {}
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) != 3 or not all(parts) or len(parts[1]) != 64:
            continue
        users[parts[0]] = UiUser(name=parts[0], password_sha256=parts[1].lower(), role=parts[2])
    return users


def check_password(user: UiUser, password: str) -> bool:
    return hmac.compare_digest(user.password_sha256, hash_password(password))


def _mac(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign(user: str, secret: str, *, ttl_s: int = SESSION_TTL_S, now: float | None = None) -> str:
    expiry = int((now if now is not None else time.time()) + ttl_s)
    payload = f"{user}|{expiry}"
    token = f"{payload}|{_mac(payload, secret)}"
    return base64.urlsafe_b64encode(token.encode()).decode()


def verify(token: str, secret: str, *, now: float | None = None) -> str | None:
    """The user of a valid, unexpired session token; None otherwise."""
    try:
        user, expiry, mac = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    if not secret or not hmac.compare_digest(mac, _mac(f"{user}|{expiry}", secret)):
        return None
    try:
        if int(expiry) < (now if now is not None else time.time()):
            return None
    except ValueError:
        return None
    return user
