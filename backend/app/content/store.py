"""Content edited in the admin dashboard: one current row per file plus every saved version.

The files in content/ are the defaults. A row replaces one of them, adds an internal document, or (for an
internal document) marks it deleted. Saves are checked against the version the editor started from, so two
people editing at once cannot silently overwrite each other.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

import asyncpg


class EditConflict(Exception):
    """Someone saved this file after the editor opened it."""


@dataclass
class ContentFile:
    path: str
    body: str
    version: int
    deleted: bool = False
    updated_by: str | None = None
    updated_at: datetime | None = None


@dataclass
class ContentVersion:
    version: int
    deleted: bool
    note: str
    updated_by: str | None
    created_at: datetime


class ContentStore(ABC):
    @abstractmethod
    async def all(self) -> list[ContentFile]: ...
    @abstractmethod
    async def get(self, path: str) -> ContentFile | None: ...
    @abstractmethod
    async def save(self, path: str, body: str, *, base_version: int, user_id: str | None, note: str = "",
                   deleted: bool = False) -> ContentFile:
        """Store a new version. base_version: the version the editor started from (0 = the default file).
        Raises EditConflict if it is no longer the current one."""
    @abstractmethod
    async def reset(self, path: str, *, base_version: int, user_id: str | None) -> None:
        """Back to the default file (the history keeps everything)."""
    @abstractmethod
    async def history(self, path: str) -> list[ContentVersion]: ...
    @abstractmethod
    async def version_body(self, path: str, version: int) -> str | None: ...
    @abstractmethod
    async def stamp(self) -> str:
        """Changes whenever any content changes (other servers use it to notice an edit)."""


def _uuid(v: str | None) -> UUID | None:
    try:
        return UUID(v) if v else None
    except ValueError:
        return None


class PgContentStore(ContentStore):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    def _file(r: asyncpg.Record) -> ContentFile:
        return ContentFile(r["path"], r["body"], r["version"], r["deleted"],
                           str(r["updated_by"]) if r["updated_by"] else None, r["updated_at"])

    async def all(self) -> list[ContentFile]:
        return [self._file(r) for r in await self.pool.fetch("SELECT * FROM content_files ORDER BY path")]

    async def get(self, path: str) -> ContentFile | None:
        r = await self.pool.fetchrow("SELECT * FROM content_files WHERE path = $1", path)
        return self._file(r) if r else None

    async def _current(self, conn: asyncpg.Connection, path: str) -> tuple[int, int]:
        """(current version, 0 = the default file; last version ever saved)."""
        cur = await conn.fetchval("SELECT version FROM content_files WHERE path = $1 FOR UPDATE", path)
        last = await conn.fetchval("SELECT coalesce(max(version), 0) FROM content_history WHERE path = $1", path)
        return int(cur or 0), int(last or 0)

    async def save(self, path: str, body: str, *, base_version: int, user_id: str | None, note: str = "",
                   deleted: bool = False) -> ContentFile:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"content:{path}")
            current, last = await self._current(conn, path)
            if current != base_version:
                raise EditConflict(path)
            version = last + 1
            r = await conn.fetchrow(
                "INSERT INTO content_files (path, body, version, deleted, updated_by, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, now()) ON CONFLICT (path) DO UPDATE SET body = $2, version = $3, "
                "deleted = $4, updated_by = $5, updated_at = now() RETURNING *",
                path, body, version, deleted, _uuid(user_id))
            await conn.execute("INSERT INTO content_history (path, version, body, deleted, note, updated_by) "
                               "VALUES ($1, $2, $3, $4, $5, $6)", path, version, body, deleted, note,
                               _uuid(user_id))
            return self._file(r)

    async def reset(self, path: str, *, base_version: int, user_id: str | None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"content:{path}")
            current, last = await self._current(conn, path)
            if current != base_version:
                raise EditConflict(path)
            await conn.execute("DELETE FROM content_files WHERE path = $1", path)
            await conn.execute("INSERT INTO content_history (path, version, body, note, updated_by) "
                               "VALUES ($1, $2, '', 'back to the default file', $3)", path, last + 1,
                               _uuid(user_id))

    async def history(self, path: str) -> list[ContentVersion]:
        rows = await self.pool.fetch("SELECT version, deleted, note, updated_by, created_at FROM content_history "
                                     "WHERE path = $1 ORDER BY version DESC LIMIT 50", path)
        return [ContentVersion(r["version"], r["deleted"], r["note"],
                               str(r["updated_by"]) if r["updated_by"] else None, r["created_at"]) for r in rows]

    async def version_body(self, path: str, version: int) -> str | None:
        return await self.pool.fetchval("SELECT body FROM content_history WHERE path = $1 AND version = $2",
                                        path, version)

    async def stamp(self) -> str:
        r = await self.pool.fetchrow("SELECT count(*) AS n, coalesce(max(id), 0) AS last FROM content_history")
        return f"{r['n']}:{r['last']}"


class MemoryContentStore(ContentStore):
    def __init__(self) -> None:
        self.files: dict[str, ContentFile] = {}
        self.versions: dict[str, list[tuple[ContentVersion, str]]] = {}

    async def all(self) -> list[ContentFile]:
        return [replace(f) for _, f in sorted(self.files.items())]

    async def get(self, path: str) -> ContentFile | None:
        f = self.files.get(path)
        return replace(f) if f else None

    def _record(self, path: str, body: str, deleted: bool, note: str, user_id: str | None) -> int:
        history = self.versions.setdefault(path, [])
        version = (history[-1][0].version if history else 0) + 1
        history.append((ContentVersion(version, deleted, note, user_id, datetime.now(UTC)), body))
        return version

    async def save(self, path: str, body: str, *, base_version: int, user_id: str | None, note: str = "",
                   deleted: bool = False) -> ContentFile:
        current = self.files[path].version if path in self.files else 0
        if current != base_version:
            raise EditConflict(path)
        version = self._record(path, body, deleted, note, user_id)
        self.files[path] = ContentFile(path, body, version, deleted, user_id, datetime.now(UTC))
        return replace(self.files[path])

    async def reset(self, path: str, *, base_version: int, user_id: str | None) -> None:
        current = self.files[path].version if path in self.files else 0
        if current != base_version:
            raise EditConflict(path)
        self.files.pop(path, None)
        self._record(path, "", False, "back to the default file", user_id)

    async def history(self, path: str) -> list[ContentVersion]:
        return [v for v, _ in reversed(self.versions.get(path, []))][:50]

    async def version_body(self, path: str, version: int) -> str | None:
        return next((b for v, b in self.versions.get(path, []) if v.version == version), None)

    async def stamp(self) -> str:
        return str(sum(len(v) for v in self.versions.values()))
