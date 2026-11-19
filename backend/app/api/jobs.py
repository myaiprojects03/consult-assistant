"""Endpoints QStash calls when JOB_RUNNER=qstash (serverless hosting). Every request must carry a valid
Upstash signature for this exact URL and body; anything else is rejected. With the local runner these
endpoints do not exist (404).

POST /api/jobs/run     {"job_id": "..."}  run one job that is due
POST /api/jobs/sweep   hourly safety net: queue the daily clean-up, run anything due, hand QStash jobs that
                       are now within reach
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.jobs.handlers import schedule_daily
from app.jobs.qstash import verify_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def _verified_body(request: Request, path: str) -> bytes:
    s = get_settings()
    if s.job_runner != "qstash":
        raise HTTPException(404, "Not found.")
    body = await request.body()
    url = f"{s.public_api_url.rstrip('/')}{path}"
    if not verify_signature(request.headers.get("upstash-signature", ""), body, url,
                            [s.qstash_current_signing_key, s.qstash_next_signing_key]):
        log.warning("rejected an unsigned or invalid call to %s", path)
        raise HTTPException(401, "Invalid signature.")
    return body


@router.post("/run")
async def run(request: Request) -> dict[str, Any]:
    body = await _verified_body(request, "/api/jobs/run")
    try:
        job_id = str(json.loads(body or b"{}").get("job_id", ""))
    except (json.JSONDecodeError, AttributeError) as exc:
        raise HTTPException(422, "Expected {\"job_id\": ...}.") from exc
    # Always 200 once verified: retries are scheduled by the worker itself, not by QStash redelivery.
    return {"outcome": await request.app.state.worker.run_by_id(job_id)}


@router.post("/sweep")
async def sweep(request: Request) -> dict[str, Any]:
    await _verified_body(request, "/api/jobs/sweep")
    await schedule_daily(request.app.state.jobs)          # the nightly privacy clean-up, once a day
    return await request.app.state.worker.sweep()
