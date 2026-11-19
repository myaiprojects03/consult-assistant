"""Upstash QStash: on serverless hosting there is no always-on worker, so each job is handed to QStash,
which calls POST /api/jobs/run at the job's time (up to 7 days ahead on the free plan). An hourly QStash
schedule calls /api/jobs/sweep as a safety net: it runs anything that is due and hands over jobs that
were too far ahead or whose hand-over failed. Every call from QStash is signed and checked here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.integrations.base import IntegrationError, call
from app.jobs.store import Job

MAX_DELAY = timedelta(days=7) - timedelta(hours=1)   # free plan limit, with a margin
SWEEP_SCHEDULE_ID = "consult-assistant-sweep"
KEEP_AWAKE_SCHEDULE_ID = "consult-assistant-keep-awake"


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_signature(token: str, body: bytes, url: str, keys: list[str], *, now: float | None = None,
                     leeway_s: int = 30) -> bool:
    """Check an Upstash-Signature JWT (HS256): signed with the current or next signing key, issued by
    Upstash, for this URL, not expired, and carrying the SHA-256 of exactly this body."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        if json.loads(_b64url_decode(header_b64)).get("alg") != "HS256":
            return False
        signed = f"{header_b64}.{payload_b64}".encode()
        sig = _b64url_decode(sig_b64)
        if not any(k and hmac.compare_digest(hmac.new(k.encode(), signed, hashlib.sha256).digest(), sig)
                   for k in keys):
            return False
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return False
    now = time.time() if now is None else now
    body_hash = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
    return (claims.get("iss") == "Upstash"
            and claims.get("sub", "").rstrip("/") == url.rstrip("/")
            and now <= float(claims.get("exp", 0)) + leeway_s
            and now >= float(claims.get("nbf", 0)) - leeway_s
            and str(claims.get("body", "")).rstrip("=") == body_hash)


class QStash:
    def __init__(self, base_url: str, token: str, public_api_url: str, client: httpx.AsyncClient | None = None):
        if not token or not public_api_url:
            raise IntegrationError("QStash: set QSTASH_TOKEN and PUBLIC_API_URL (or use JOB_RUNNER=local).",
                                   retryable=False)
        self.base = base_url.rstrip("/")
        self.run_url = f"{public_api_url.rstrip('/')}/api/jobs/run"
        self.sweep_url = f"{public_api_url.rstrip('/')}/api/jobs/sweep"
        self.health_url = f"{public_api_url.rstrip('/')}/health"
        self.client = client or httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"})

    def can_schedule(self, job: Job, now: datetime | None = None) -> bool:
        return job.run_at - (now or datetime.now(UTC)) <= MAX_DELAY

    async def publish(self, job: Job) -> None:
        headers = {"Content-Type": "application/json", "Upstash-Retries": "3",
                   # same job and time: QStash drops the copy (e.g. a sweep re-sending it)
                   "Upstash-Deduplication-Id": f"{job.id}-{int(job.run_at.timestamp())}"}
        if job.run_at > datetime.now(UTC):
            headers["Upstash-Not-Before"] = str(int(job.run_at.timestamp()))
        await call(self.client, "POST", f"{self.base}/v2/publish/{self.run_url}", service="QStash",
                   headers=headers, content=json.dumps({"job_id": job.id}))

    async def ensure_sweep_schedule(self, cron: str = "0 * * * *") -> None:
        await call(self.client, "POST", f"{self.base}/v2/schedules/{self.sweep_url}", service="QStash",
                   headers={"Upstash-Cron": cron, "Upstash-Schedule-Id": SWEEP_SCHEDULE_ID,
                            "Content-Type": "application/json"}, content="{}")

    async def ensure_keep_awake(self, cron: str = "*/10 * * * *") -> None:
        """Free hosting that sleeps after 15 idle minutes (Render free): a GET /health every 10 minutes keeps
        the first chat reply and phone calls fast. Not needed on an always-on instance."""
        await call(self.client, "POST", f"{self.base}/v2/schedules/{self.health_url}", service="QStash",
                   headers={"Upstash-Cron": cron, "Upstash-Schedule-Id": KEEP_AWAKE_SCHEDULE_ID,
                            "Upstash-Method": "GET", "Upstash-Retries": "0"})

    async def remove_keep_awake(self) -> None:
        await call(self.client, "DELETE", f"{self.base}/v2/schedules/{KEEP_AWAKE_SCHEDULE_ID}", service="QStash",
                   ok=(200, 204, 404))                       # 404: there was none

    async def aclose(self) -> None:
        await self.client.aclose()
