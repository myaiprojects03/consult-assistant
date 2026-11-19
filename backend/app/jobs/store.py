"""Durable job queue in Postgres (the "outbox"): every outside action is first written here, in the same
database as the record it belongs to, then carried out by a worker. Nothing is lost if a service is down
or the server restarts, and nothing runs twice:

- `key` is an idempotency key: enqueueing the same key again returns the existing job.
- A worker claims a job with a lease (FOR UPDATE SKIP LOCKED), so two workers never run the same job;
  a job whose worker died is picked up again when its lease runs out.
- Failures are retried with growing delays; after `max_attempts` the job is marked failed and alerted.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg

LEASE = timedelta(minutes=2)


@dataclass
class Job:
    id: str
    kind: str
    key: str
    payload: dict[str, Any] = field(default_factory=dict)
    ref: str = ""
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 6
    run_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str = ""
    locked_until: datetime | None = None
    dispatched_at: datetime | None = None

    @property
    def last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


class JobStore(ABC):
    @abstractmethod
    async def add(self, job: Job, *, coalesce: bool = False) -> tuple[Job, bool]:
        """Insert unless a job with the same key exists (returns it, created=False). With coalesce, an
        existing *pending* job of the same kind and ref is returned instead (e.g. one CRM sync is enough)."""
    @abstractmethod
    async def claim(self, job_id: str | None = None, now: datetime | None = None) -> Job | None:
        """Take one due job (a given one, or the earliest) and lease it to this worker."""
    @abstractmethod
    async def finish(self, job_id: str) -> None: ...
    @abstractmethod
    async def retry(self, job_id: str, error: str, run_at: datetime) -> Job: ...
    @abstractmethod
    async def fail(self, job_id: str, error: str) -> None: ...
    @abstractmethod
    async def cancel(self, ref: str, kinds: list[str]) -> int:
        """Cancel pending jobs about `ref` (e.g. the reminders of a booking that moved)."""
    @abstractmethod
    async def mark_dispatched(self, job_id: str) -> None: ...
    @abstractmethod
    async def undispatched(self, until: datetime, limit: int = 200) -> list[Job]:
        """Pending jobs due before `until` that were never handed to QStash."""
    @abstractmethod
    async def counts(self) -> dict[str, int]: ...


def _row(r: asyncpg.Record) -> Job:
    payload = r["payload"]
    return Job(id=str(r["id"]), kind=r["kind"], key=r["key"],
               payload=json.loads(payload) if isinstance(payload, str) else dict(payload), ref=r["ref"],
               status=r["status"], attempts=r["attempts"], max_attempts=r["max_attempts"], run_at=r["run_at"],
               last_error=r["last_error"], locked_until=r["locked_until"], dispatched_at=r["dispatched_at"])


class PgJobStore(JobStore):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def add(self, job: Job, *, coalesce: bool = False) -> tuple[Job, bool]:
        if coalesce:
            r = await self.pool.fetchrow("SELECT * FROM jobs WHERE ref = $1 AND kind = $2 AND status = 'pending' "
                                         "LIMIT 1", job.ref, job.kind)
            if r:
                return _row(r), False
        r = await self.pool.fetchrow(
            "INSERT INTO jobs (kind, key, ref, payload, run_at, max_attempts) VALUES ($1, $2, $3, $4::jsonb, $5, $6) "
            "ON CONFLICT (key) DO NOTHING RETURNING *",
            job.kind, job.key, job.ref, json.dumps(job.payload), job.run_at, job.max_attempts)
        if r:
            return _row(r), True
        existing = await self.pool.fetchrow("SELECT * FROM jobs WHERE key = $1", job.key)
        return _row(existing), False

    async def claim(self, job_id: str | None = None, now: datetime | None = None) -> Job | None:
        now = now or datetime.now(UTC)
        if job_id:
            try:
                jid = UUID(job_id)
            except ValueError:
                return None
            r = await self.pool.fetchrow(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_until = $2 WHERE id = ("
                "SELECT id FROM jobs WHERE id = $3 AND ((status = 'pending' AND run_at <= $1) OR "
                "(status = 'running' AND locked_until < $1)) FOR UPDATE SKIP LOCKED) RETURNING *",
                now, now + LEASE, jid)
        else:
            r = await self.pool.fetchrow(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_until = $2 WHERE id = ("
                "SELECT id FROM jobs WHERE (status = 'pending' AND run_at <= $1) OR "
                "(status = 'running' AND locked_until < $1) ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING *", now, now + LEASE)
        return _row(r) if r else None

    async def finish(self, job_id: str) -> None:
        await self.pool.execute("UPDATE jobs SET status = 'done', finished_at = now(), locked_until = NULL, "
                                "last_error = '' WHERE id = $1", UUID(job_id))

    async def retry(self, job_id: str, error: str, run_at: datetime) -> Job:
        r = await self.pool.fetchrow(
            "UPDATE jobs SET status = 'pending', run_at = $2, last_error = $3, locked_until = NULL, "
            "dispatched_at = NULL WHERE id = $1 RETURNING *", UUID(job_id), run_at, error[:1000])
        return _row(r)

    async def fail(self, job_id: str, error: str) -> None:
        await self.pool.execute("UPDATE jobs SET status = 'failed', finished_at = now(), locked_until = NULL, "
                                "last_error = $2 WHERE id = $1", UUID(job_id), error[:1000])

    async def cancel(self, ref: str, kinds: list[str]) -> int:
        res = await self.pool.execute("UPDATE jobs SET status = 'cancelled', finished_at = now() "
                                      "WHERE ref = $1 AND kind = ANY($2) AND status = 'pending'", ref, kinds)
        return int(res.split()[-1])

    async def mark_dispatched(self, job_id: str) -> None:
        await self.pool.execute("UPDATE jobs SET dispatched_at = now() WHERE id = $1", UUID(job_id))

    async def undispatched(self, until: datetime, limit: int = 200) -> list[Job]:
        rows = await self.pool.fetch("SELECT * FROM jobs WHERE status = 'pending' AND dispatched_at IS NULL "
                                     "AND run_at <= $1 ORDER BY run_at LIMIT $2", until, limit)
        return [_row(r) for r in rows]

    async def counts(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT status, count(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}


class MemoryJobStore(JobStore):
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def _copy(self, j: Job) -> Job:
        return replace(j, payload=dict(j.payload))

    async def add(self, job: Job, *, coalesce: bool = False) -> tuple[Job, bool]:
        if coalesce:
            for j in self.jobs.values():
                if j.ref == job.ref and j.kind == job.kind and j.status == "pending":
                    return self._copy(j), False
        for j in self.jobs.values():
            if j.key == job.key:
                return self._copy(j), False
        stored = replace(job, id=job.id or str(uuid4()))
        self.jobs[stored.id] = stored
        return self._copy(stored), True

    async def claim(self, job_id: str | None = None, now: datetime | None = None) -> Job | None:
        now = now or datetime.now(UTC)

        def due(j: Job) -> bool:
            return (j.status == "pending" and j.run_at <= now) or (
                j.status == "running" and j.locked_until is not None and j.locked_until < now)
        candidates = [self.jobs[job_id]] if job_id in self.jobs else ([] if job_id else list(self.jobs.values()))
        ready = sorted((j for j in candidates if due(j)), key=lambda j: j.run_at)
        if not ready:
            return None
        j = ready[0]
        j.status, j.attempts, j.locked_until = "running", j.attempts + 1, now + LEASE
        return self._copy(j)

    async def finish(self, job_id: str) -> None:
        j = self.jobs[job_id]
        j.status, j.locked_until, j.last_error = "done", None, ""

    async def retry(self, job_id: str, error: str, run_at: datetime) -> Job:
        j = self.jobs[job_id]
        j.status, j.run_at, j.last_error, j.locked_until, j.dispatched_at = "pending", run_at, error, None, None
        return self._copy(j)

    async def fail(self, job_id: str, error: str) -> None:
        j = self.jobs[job_id]
        j.status, j.last_error, j.locked_until = "failed", error, None

    async def cancel(self, ref: str, kinds: list[str]) -> int:
        n = 0
        for j in self.jobs.values():
            if j.ref == ref and j.kind in kinds and j.status == "pending":
                j.status = "cancelled"
                n += 1
        return n

    async def mark_dispatched(self, job_id: str) -> None:
        self.jobs[job_id].dispatched_at = datetime.now(UTC)

    async def undispatched(self, until: datetime, limit: int = 200) -> list[Job]:
        out = [j for j in self.jobs.values() if j.status == "pending" and j.dispatched_at is None and j.run_at <= until]
        return [self._copy(j) for j in sorted(out, key=lambda j: j.run_at)[:limit]]

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for j in self.jobs.values():
            out[j.status] = out.get(j.status, 0) + 1
        return out
