"""Calls: one row per phone or browser voice call. What was said is stored as the call's conversation
(the same messages table as chat), so transcripts, summaries, leads and handovers work the same way."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg

FINAL_STATUSES = ("completed", "busy", "no_answer", "failed", "canceled")


@dataclass
class Call:
    id: str
    direction: str                        # inbound | outbound | web
    call_sid: str | None = None
    request_key: str | None = None
    from_number: str = ""
    to_number: str = ""
    conversation_id: str | None = None
    purpose: str = ""
    contact_name: str = ""
    requested_by: str | None = None
    status: str = "queued"
    outcome: str = ""
    transfer_to: str = ""
    error: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: int | None = None

    @property
    def caller_number(self) -> str:
        """The other person's number: who called us, or whom we called."""
        return self.to_number if self.direction == "outbound" else self.from_number


class CallStore(ABC):
    @abstractmethod
    async def create(self, call: Call) -> tuple[Call, bool]:
        """Insert; if the call_sid or request_key already exists, return that call. (call, created)."""
    @abstractmethod
    async def get(self, call_id: str) -> Call | None: ...
    @abstractmethod
    async def by_sid(self, call_sid: str) -> Call | None: ...
    @abstractmethod
    async def update(self, call_id: str, **fields: object) -> None:
        """Set any of: call_sid, conversation_id, status, outcome, transfer_to, error, started_at, ended_at,
        duration_s. A finished call keeps its final status."""
    @abstractmethod
    async def recent(self, limit: int = 50) -> list[Call]: ...
    @abstractmethod
    async def claim(self, call_id: str, from_statuses: tuple[str, ...], to_status: str) -> bool:
        """Move the call to `to_status` only if it is in one of `from_statuses` (one winner)."""


UPDATABLE = {"call_sid", "conversation_id", "status", "outcome", "transfer_to", "error", "started_at", "ended_at",
             "duration_s"}


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def _call(r: asyncpg.Record) -> Call:
    return Call(str(r["id"]), r["direction"], r["call_sid"], r["request_key"], r["from_number"], r["to_number"],
                str(r["conversation_id"]) if r["conversation_id"] else None, r["purpose"], r["contact_name"],
                str(r["requested_by"]) if r["requested_by"] else None, r["status"], r["outcome"], r["transfer_to"],
                r["error"], r["created_at"], r["started_at"], r["ended_at"], r["duration_s"])


class PgCallStore(CallStore):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, call: Call) -> tuple[Call, bool]:
        r = await self.pool.fetchrow(
            "INSERT INTO calls (call_sid, request_key, direction, from_number, to_number, conversation_id, purpose, "
            "contact_name, requested_by, status) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
            "ON CONFLICT DO NOTHING RETURNING *", call.call_sid, call.request_key, call.direction, call.from_number,
            call.to_number, _uuid(call.conversation_id), call.purpose, call.contact_name, _uuid(call.requested_by),
            call.status)
        if r:
            return _call(r), True
        existing = (await self.by_sid(call.call_sid) if call.call_sid else None) or await self.pool.fetchrow(
            "SELECT * FROM calls WHERE request_key = $1", call.request_key)
        if existing is None:
            raise RuntimeError("call insert conflicted but no existing call was found")
        return (existing if isinstance(existing, Call) else _call(existing)), False

    async def get(self, call_id: str) -> Call | None:
        cid = _uuid(call_id)
        r = await self.pool.fetchrow("SELECT * FROM calls WHERE id = $1", cid) if cid else None
        return _call(r) if r else None

    async def by_sid(self, call_sid: str) -> Call | None:
        r = await self.pool.fetchrow("SELECT * FROM calls WHERE call_sid = $1", call_sid)
        return _call(r) if r else None

    async def update(self, call_id: str, **fields: object) -> None:
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        if not fields:
            return
        values = [_uuid(str(v)) if k == "conversation_id" and v else v for k, v in fields.items()]
        # column names come from the UPDATABLE allowlist above; values are always parameters
        sets = ", ".join(
            f"status = CASE WHEN status IN ('completed', 'busy', 'no_answer', 'failed', 'canceled') THEN status "
            f"ELSE ${i} END" if k == "status" else f"{k} = ${i}" for i, k in enumerate(fields, 2))
        await self.pool.execute(f"UPDATE calls SET {sets} WHERE id = $1", _uuid(call_id), *values)  # noqa: S608

    async def recent(self, limit: int = 50) -> list[Call]:
        return [_call(r) for r in await self.pool.fetch("SELECT * FROM calls ORDER BY created_at DESC LIMIT $1",
                                                        limit)]

    async def claim(self, call_id: str, from_statuses: tuple[str, ...], to_status: str) -> bool:
        res = await self.pool.execute("UPDATE calls SET status = $2 WHERE id = $1 AND status = ANY($3)",
                                      _uuid(call_id), to_status, list(from_statuses))
        return res.endswith(" 1")


class MemoryCallStore(CallStore):
    def __init__(self) -> None:
        self.calls: dict[str, Call] = {}

    async def create(self, call: Call) -> tuple[Call, bool]:
        for c in self.calls.values():
            if (call.call_sid and c.call_sid == call.call_sid) or (call.request_key and
                                                                     c.request_key == call.request_key):
                return replace(c), False
        new = replace(call, id=str(uuid4()), created_at=datetime.now(UTC))
        self.calls[new.id] = new
        return replace(new), True

    async def get(self, call_id: str) -> Call | None:
        c = self.calls.get(call_id)
        return replace(c) if c else None

    async def by_sid(self, call_sid: str) -> Call | None:
        return next((replace(c) for c in self.calls.values() if c.call_sid == call_sid), None)

    async def update(self, call_id: str, **fields: object) -> None:
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        c = self.calls[call_id]
        if "status" in fields and c.status in FINAL_STATUSES:
            fields = {k: v for k, v in fields.items() if k != "status"}
        for k, v in fields.items():
            setattr(c, k, v)

    async def recent(self, limit: int = 50) -> list[Call]:
        return [replace(c) for c in sorted(self.calls.values(), key=lambda c: c.created_at or datetime.min.replace(
            tzinfo=UTC), reverse=True)[:limit]]

    async def claim(self, call_id: str, from_statuses: tuple[str, ...], to_status: str) -> bool:
        c = self.calls.get(call_id)
        if c is None or c.status not in from_statuses:
            return False
        c.status = to_status
        return True
