"""Background jobs: idempotent enqueueing, retries, failure alerts, leases, QStash signing and endpoints."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.integrations.base import IntegrationError
from app.jobs.qstash import QStash, verify_signature
from app.jobs.runner import Jobs, SkipJob, Worker, backoff
from app.jobs.store import Job, MemoryJobStore


def run(coro):
    return asyncio.run(coro)


def make_worker(handler, store=None):
    jobs = Jobs(store or MemoryJobStore(), "manual")
    failures: list[tuple[str, str]] = []

    async def on_failure(job, error):
        failures.append((job.kind, error))
    return Worker(jobs, {"t": handler}, None, on_failure), jobs, failures


def test_same_key_is_one_job_and_coalesce_keeps_one_pending():
    jobs = Jobs(MemoryJobStore(), "manual")
    a = run(jobs.enqueue("t", "k1", {"x": 1}))
    b = run(jobs.enqueue("t", "k1", {"x": 2}))
    assert a.id == b.id and len(jobs.store.jobs) == 1
    c1 = run(jobs.enqueue("crm.sync", "crm:1", {}, ref="conv", coalesce=True))
    c2 = run(jobs.enqueue("crm.sync", "crm:2", {}, ref="conv", coalesce=True))
    assert c1.id == c2.id


def test_success_and_skip_mark_the_job_done():
    calls = []

    async def ok(ctx, job):
        calls.append(job.payload)

    async def skip(ctx, job):
        raise SkipJob("no longer needed")
    w, jobs, _ = make_worker(ok)
    run(jobs.enqueue("t", "a", {"n": 1}))
    assert run(w.run_due()) == 1 and calls == [{"n": 1}]
    assert next(iter(jobs.store.jobs.values())).status == "done"
    w.handlers["t"] = skip
    run(jobs.enqueue("t", "b", {}))
    run(w.run_due())
    assert {j.status for j in jobs.store.jobs.values()} == {"done"}


def test_temporary_errors_retry_later_then_fail_with_an_alert():
    async def flaky(ctx, job):
        raise IntegrationError("service down")
    w, jobs, failures = make_worker(flaky)
    job = run(jobs.enqueue("t", "a", {}, max_attempts=2))
    assert run(w.run_due()) == 1
    stored = jobs.store.jobs[job.id]
    assert stored.status == "pending" and stored.run_at > datetime.now(UTC) + timedelta(seconds=20)
    assert run(w.run_due()) == 0                      # not due yet
    stored.run_at = datetime.now(UTC)
    run(w.run_due())
    assert stored.status == "failed" and failures == [("t", "service down")]


def test_permanent_errors_fail_at_once():
    async def bad_key(ctx, job):
        raise IntegrationError("access refused", retryable=False)
    w, jobs, failures = make_worker(bad_key)
    run(jobs.enqueue("t", "a", {}))
    run(w.run_due())
    assert next(iter(jobs.store.jobs.values())).status == "failed" and len(failures) == 1


def test_a_claimed_job_is_not_run_twice_until_its_lease_ends():
    store = MemoryJobStore()
    run(store.add(Job(id="", kind="t", key="a")))
    first = run(store.claim())
    assert first is not None and run(store.claim()) is None
    later = datetime.now(UTC) + timedelta(minutes=3)        # the first worker died; its lease ran out
    again = run(store.claim(now=later))
    assert again is not None and again.id == first.id and again.attempts == 2


def test_backoff_grows_and_is_capped():
    assert 24 <= backoff(1).total_seconds() <= 36
    assert 192 <= backoff(4).total_seconds() <= 288
    assert backoff(20).total_seconds() <= 3600 * 1.2


# ---------------- QStash ----------------
def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign(key: str, url: str, body: bytes, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": "Upstash", "sub": url, "exp": now + 300, "nbf": now, "iat": now, "jti": "j1",
              "body": base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode(), **overrides}
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    sig = hmac.new(key.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    return f"{head}.{payload}.{b64(sig)}"


URL = "https://api.example.com/api/jobs/run"


def test_signature_checks():
    body = b'{"job_id":"1"}'
    keys = ["current", "next"]
    assert verify_signature(sign("current", URL, body), body, URL, keys)
    assert verify_signature(sign("next", URL, body), body, URL, keys)          # during key rotation
    assert not verify_signature(sign("other", URL, body), body, URL, keys)
    assert not verify_signature(sign("current", URL, body), b'{"job_id":"2"}', URL, keys)   # body changed
    assert not verify_signature(sign("current", "https://evil.example/api/jobs/run", body), body, URL, keys)
    assert not verify_signature(sign("current", URL, body, exp=int(time.time()) - 600), body, URL, keys)
    assert not verify_signature("not-a-token", body, URL, keys)
    assert not verify_signature(sign("current", URL, body), body, URL, ["", ""])


def test_keep_awake_schedule_is_a_get_of_health_and_can_be_removed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(404 if request.method == "DELETE" else 200, json={})
    q = QStash("https://qstash.upstash.io", "tok", "https://api.example.com",
               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run(q.ensure_keep_awake())
    run(q.remove_keep_awake())                    # none there (404): fine
    made, removed = seen
    assert str(made.url) == "https://qstash.upstash.io/v2/schedules/https://api.example.com/health"
    assert made.headers["Upstash-Method"] == "GET" and made.headers["Upstash-Cron"] == "*/10 * * * *"
    assert removed.method == "DELETE" and str(removed.url).endswith("/v2/schedules/consult-assistant-keep-awake")


def test_publish_request_and_seven_day_limit():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"messageId": "m1", "deduplicated": False})
    q = QStash("https://qstash.upstash.io", "tok", "https://api.example.com",
               client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                        headers={"Authorization": "Bearer tok"}))
    later = datetime.now(UTC) + timedelta(hours=2)
    job = Job(id="j1", kind="t", key="k", run_at=later)
    run(q.publish(job))
    r = seen[0]
    assert str(r.url) == "https://qstash.upstash.io/v2/publish/https://api.example.com/api/jobs/run"
    assert r.headers["Upstash-Not-Before"] == str(int(later.timestamp()))
    assert r.headers["Upstash-Deduplication-Id"] == f"j1-{int(later.timestamp())}"
    assert json.loads(r.content) == {"job_id": "j1"} and r.headers["Authorization"] == "Bearer tok"
    assert q.can_schedule(job) and not q.can_schedule(Job(id="x", kind="t", key="y",
                                                          run_at=datetime.now(UTC) + timedelta(days=8)))


def test_job_endpoints_need_qstash_mode_and_a_signature(make_client, monkeypatch):
    client, app, _ = make_client()
    assert client.post("/api/jobs/run", json={"job_id": "x"}).status_code == 404     # local/manual runner

    from app.config import get_settings
    monkeypatch.setenv("JOB_RUNNER", "qstash")
    monkeypatch.setenv("PUBLIC_API_URL", "https://api.example.com")
    monkeypatch.setenv("QSTASH_CURRENT_SIGNING_KEY", "current")
    monkeypatch.setenv("QSTASH_NEXT_SIGNING_KEY", "next")
    get_settings.cache_clear()
    try:
        ran = []

        async def handler(ctx, job):
            ran.append(job.id)
        app.state.worker.handlers["t"] = handler
        job = asyncio.run(app.state.jobs.enqueue("t", "endpoint", {}))
        body = json.dumps({"job_id": job.id}).encode()
        unsigned = client.post("/api/jobs/run", content=body)
        assert unsigned.status_code == 401 and not ran
        ok = client.post("/api/jobs/run", content=body, headers={"Upstash-Signature": sign("current", URL, body)})
        assert ok.status_code == 200 and ok.json() == {"outcome": "done"} and ran == [job.id]
        again = client.post("/api/jobs/run", content=body, headers={"Upstash-Signature": sign("current", URL, body)})
        assert again.json() == {"outcome": "not_due"} and ran == [job.id]          # a redelivery runs nothing
        sweep_url = "https://api.example.com/api/jobs/sweep"
        r = client.post("/api/jobs/sweep", content=b"{}",
                        headers={"Upstash-Signature": sign("current", sweep_url, b"{}")})
        assert r.status_code == 200 and r.json()["ran"] == 1          # today's privacy clean-up, queued and run
        r = client.post("/api/jobs/sweep", content=b"{}",
                        headers={"Upstash-Signature": sign("current", sweep_url, b"{}")})
        assert r.json()["ran"] == 0                                    # once a day
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
