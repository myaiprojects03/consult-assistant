"""Enqueueing and running jobs.

`Jobs.enqueue` writes the job, then wakes the in-process worker (JOB_RUNNER=local) or hands it to QStash
(JOB_RUNNER=qstash). `Worker` runs one job with its handler, then marks it done, schedules a retry with a
growing delay, or marks it failed and raises an alert. Handlers must be safe to run twice (an at-least-once
system): they read the current state of the record and use fixed ids for anything they create.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.integrations.base import IntegrationError
from app.jobs.qstash import MAX_DELAY, QStash
from app.jobs.store import Job, JobStore

log = logging.getLogger(__name__)
Handler = Callable[[Any, Job], Awaitable[None]]
FailureHook = Callable[[Job, str], Awaitable[None]]


class SkipJob(Exception):  # noqa: N818 - a signal, not an error
    """Raised by a handler when the job no longer applies (e.g. a reminder for a call that moved)."""


def backoff(attempt: int) -> timedelta:
    """30 s, 1 min, 2 min, 4 min, 8 min ... capped at 1 hour, with jitter so retries do not bunch up."""
    base = min(30 * 2 ** max(attempt - 1, 0), 3600)
    return timedelta(seconds=base * random.uniform(0.8, 1.2))   # noqa: S311 - not security related


class Jobs:
    def __init__(self, store: JobStore, mode: str = "local", qstash: QStash | None = None):
        if mode not in ("local", "qstash", "manual"):
            raise ValueError(f"Unknown JOB_RUNNER '{mode}' (local, qstash, manual).")
        if mode == "qstash" and qstash is None:
            raise ValueError("JOB_RUNNER=qstash needs the QStash settings.")
        self.store, self.mode, self.qstash = store, mode, qstash
        self.wake = asyncio.Event()

    async def enqueue(self, kind: str, key: str, payload: dict[str, Any], *, ref: str = "",
                      run_at: datetime | None = None, coalesce: bool = False, max_attempts: int = 6) -> Job:
        job, created = await self.store.add(Job(id="", kind=kind, key=key, payload=payload, ref=ref,
                                                run_at=run_at or datetime.now(UTC), max_attempts=max_attempts),
                                            coalesce=coalesce)
        if created:
            await self.dispatch(job)
        return job

    async def dispatch(self, job: Job) -> None:
        if self.mode == "local":
            self.wake.set()
        elif self.mode == "qstash" and self.qstash and self.qstash.can_schedule(job):
            try:
                await self.qstash.publish(job)
                await self.store.mark_dispatched(job.id)
            except IntegrationError as exc:   # the hourly sweep hands it over later
                log.warning("could not hand job %s to QStash: %s", job.id, exc)


class Worker:
    def __init__(self, jobs: Jobs, handlers: dict[str, Handler], context: Any, on_failure: FailureHook):
        self.jobs, self.handlers, self.context, self.on_failure = jobs, handlers, context, on_failure

    async def run(self, job: Job) -> str:
        """Run a claimed job. Returns done, skipped, retry or failed."""
        handler = self.handlers.get(job.kind)
        try:
            if handler is None:
                raise IntegrationError(f"no handler for job kind '{job.kind}'", retryable=False)
            await handler(self.context, job)
        except SkipJob as reason:
            await self.jobs.store.finish(job.id)
            log.info("job %s (%s) skipped: %s", job.id, job.kind, reason)
            return "skipped"
        except Exception as exc:
            retryable = getattr(exc, "retryable", True)
            error = str(exc) or type(exc).__name__
            if not isinstance(exc, IntegrationError):
                log.exception("job %s (%s) crashed", job.id, job.kind)
            if retryable and not job.last_attempt:
                updated = await self.jobs.store.retry(job.id, error, datetime.now(UTC) + backoff(job.attempts))
                await self.jobs.dispatch(updated)
                log.warning("job %s (%s) attempt %d failed, will retry: %s", job.id, job.kind, job.attempts, error)
                return "retry"
            await self.jobs.store.fail(job.id, error)
            log.error("job %s (%s) failed: %s", job.id, job.kind, error)
            await self.on_failure(job, error)
            return "failed"
        await self.jobs.store.finish(job.id)
        return "done"

    async def run_by_id(self, job_id: str) -> str:
        job = await self.jobs.store.claim(job_id)
        return await self.run(job) if job else "not_due"

    async def run_due(self, limit: int = 50, budget_s: float | None = None) -> int:
        started, n = time.monotonic(), 0
        while n < limit and (budget_s is None or time.monotonic() - started < budget_s):
            job = await self.jobs.store.claim()
            if job is None:
                break
            await self.run(job)
            n += 1
        return n

    async def sweep(self, budget_s: float = 40) -> dict[str, int]:
        """Safety net (hourly on serverless): run what is due, then hand QStash what is now within reach."""
        ran = await self.run_due(limit=200, budget_s=budget_s)
        handed = 0
        if self.jobs.mode == "qstash":
            for job in await self.jobs.store.undispatched(datetime.now(UTC) + MAX_DELAY):
                await self.jobs.dispatch(job)
                handed += 1
        return {"ran": ran, "handed_to_qstash": handed}


async def local_loop(worker: Worker, idle_s: float = 5.0) -> None:
    """The in-process worker for JOB_RUNNER=local: runs jobs as soon as they are queued (or come due)."""
    while True:
        try:
            if await worker.run_due() == 0:
                worker.jobs.wake.clear()
                try:
                    await asyncio.wait_for(worker.jobs.wake.wait(), timeout=idle_s)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:   # e.g. the database restarting: wait and carry on
            log.exception("job worker error")
            await asyncio.sleep(idle_s)
