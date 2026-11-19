"""Staff accounts for the internal assistant and the admin dashboard (role admin).

Passwords are hashed with scrypt (salted, memory-hard); sessions are random tokens stored only as a
SHA-256 hash, so a database leak gives no usable session. Admins see every client; a consultant sees
only the clients they are given access to.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

ROLES = ("admin", "consultant")
SESSION_IDLE = timedelta(hours=12)
SESSION_MAX = timedelta(days=7)
MIN_PASSWORD = 12
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), dklen=32, **_SCRYPT)
        return hmac.compare_digest(key, base64.b64decode(key_b64))
    except (ValueError, TypeError):
        return False


# Used when an email is unknown, so a wrong email and a wrong password take the same time.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_session_token() -> tuple[str, str]:
    """(token for the browser, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class StaffUser:
    id: str
    email: str
    name: str
    role: str
    consultant_id: str = ""
    active: bool = True

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class StaffStore(ABC):
    @abstractmethod
    async def create_user(self, email: str, name: str, role: str, password_hash: str,
                          consultant_id: str = "") -> StaffUser: ...
    @abstractmethod
    async def user_with_hash(self, email: str) -> tuple[StaffUser, str] | None: ...
    @abstractmethod
    async def set_password(self, user_id: str, password_hash: str) -> None:
        """Also signs the user out everywhere."""
    @abstractmethod
    async def set_active(self, user_id: str, active: bool) -> None: ...
    @abstractmethod
    async def grant(self, user_id: str, client_id: str) -> None: ...
    @abstractmethod
    async def revoke(self, user_id: str, client_id: str) -> None: ...
    @abstractmethod
    async def allowed_clients(self, user: StaffUser) -> list[str] | None:
        """Client ids this user may see; None means all (admins)."""
    @abstractmethod
    async def create_session(self, user_id: str, token_hash: str) -> None: ...
    @abstractmethod
    async def session_user(self, token_hash: str) -> StaffUser | None:
        """The signed-in user, or None if the session is unknown, expired or the user is disabled."""
    @abstractmethod
    async def end_session(self, token_hash: str) -> None: ...


def _user(r: asyncpg.Record) -> StaffUser:
    return StaffUser(str(r["id"]), r["email"], r["name"], r["role"], r["consultant_id"], r["active"])


class PgStaffStore(StaffStore):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create_user(self, email: str, name: str, role: str, password_hash: str,
                          consultant_id: str = "") -> StaffUser:
        r = await self.pool.fetchrow(
            "INSERT INTO staff_users (email, name, role, consultant_id, password_hash) VALUES ($1, $2, $3, $4, $5) "
            "RETURNING *", email.strip().lower(), name, role, consultant_id, password_hash)
        return _user(r)

    async def user_with_hash(self, email: str) -> tuple[StaffUser, str] | None:
        r = await self.pool.fetchrow("SELECT * FROM staff_users WHERE email = $1", email.strip().lower())
        return (_user(r), r["password_hash"]) if r else None

    async def set_password(self, user_id: str, password_hash: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE staff_users SET password_hash = $2 WHERE id = $1", UUID(user_id), password_hash)
            await conn.execute("DELETE FROM staff_sessions WHERE user_id = $1", UUID(user_id))

    async def set_active(self, user_id: str, active: bool) -> None:
        await self.pool.execute("UPDATE staff_users SET active = $2 WHERE id = $1", UUID(user_id), active)
        if not active:
            await self.pool.execute("DELETE FROM staff_sessions WHERE user_id = $1", UUID(user_id))

    async def grant(self, user_id: str, client_id: str) -> None:
        await self.pool.execute("INSERT INTO staff_client_access (user_id, client_id) VALUES ($1, $2) "
                                "ON CONFLICT DO NOTHING", UUID(user_id), UUID(client_id))

    async def revoke(self, user_id: str, client_id: str) -> None:
        await self.pool.execute("DELETE FROM staff_client_access WHERE user_id = $1 AND client_id = $2",
                                UUID(user_id), UUID(client_id))

    async def allowed_clients(self, user: StaffUser) -> list[str] | None:
        if user.is_admin:
            return None
        rows = await self.pool.fetch("SELECT client_id FROM staff_client_access WHERE user_id = $1", UUID(user.id))
        return [str(r["client_id"]) for r in rows]

    async def create_session(self, user_id: str, token_hash: str) -> None:
        await self.pool.execute("INSERT INTO staff_sessions (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
                                token_hash, UUID(user_id), datetime.now(UTC) + SESSION_MAX)
        await self.pool.execute("UPDATE staff_users SET last_login_at = now() WHERE id = $1", UUID(user_id))

    async def session_user(self, token_hash: str) -> StaffUser | None:
        r = await self.pool.fetchrow(
            "UPDATE staff_sessions s SET last_seen_at = now() FROM staff_users u WHERE s.token_hash = $1 "
            "AND u.id = s.user_id AND u.active AND s.expires_at > now() AND s.last_seen_at > now() - $2::interval "
            "RETURNING u.*", token_hash, SESSION_IDLE)
        return _user(r) if r else None

    async def end_session(self, token_hash: str) -> None:
        await self.pool.execute("DELETE FROM staff_sessions WHERE token_hash = $1", token_hash)


class MemoryStaffStore(StaffStore):
    def __init__(self) -> None:
        self.users: dict[str, tuple[StaffUser, str]] = {}
        self.access: set[tuple[str, str]] = set()
        self.sessions: dict[str, tuple[str, datetime, datetime]] = {}   # hash -> (user, expires, last seen)

    async def create_user(self, email: str, name: str, role: str, password_hash: str,
                          consultant_id: str = "") -> StaffUser:
        email = email.strip().lower()
        if any(u.email == email for u, _ in self.users.values()):
            raise ValueError("a user with that email exists")
        u = StaffUser(str(uuid4()), email, name, role, consultant_id)
        self.users[u.id] = (u, password_hash)
        return replace(u)

    async def user_with_hash(self, email: str) -> tuple[StaffUser, str] | None:
        found = next(((u, h) for u, h in self.users.values() if u.email == email.strip().lower()), None)
        return (replace(found[0]), found[1]) if found else None

    async def set_password(self, user_id: str, password_hash: str) -> None:
        self.users[user_id] = (self.users[user_id][0], password_hash)
        self.sessions = {h: s for h, s in self.sessions.items() if s[0] != user_id}

    async def set_active(self, user_id: str, active: bool) -> None:
        self.users[user_id][0].active = active
        if not active:
            self.sessions = {h: s for h, s in self.sessions.items() if s[0] != user_id}

    async def grant(self, user_id: str, client_id: str) -> None:
        self.access.add((user_id, client_id))

    async def revoke(self, user_id: str, client_id: str) -> None:
        self.access.discard((user_id, client_id))

    async def allowed_clients(self, user: StaffUser) -> list[str] | None:
        return None if user.is_admin else [c for u, c in self.access if u == user.id]

    async def create_session(self, user_id: str, token_hash: str) -> None:
        now = datetime.now(UTC)
        self.sessions[token_hash] = (user_id, now + SESSION_MAX, now)

    async def session_user(self, token_hash: str) -> StaffUser | None:
        s = self.sessions.get(token_hash)
        now = datetime.now(UTC)
        if not s or s[1] <= now or s[2] <= now - SESSION_IDLE:
            return None
        user = self.users[s[0]][0]
        if not user.active:
            return None
        self.sessions[token_hash] = (s[0], s[1], now)
        return replace(user)

    async def end_session(self, token_hash: str) -> None:
        self.sessions.pop(token_hash, None)
