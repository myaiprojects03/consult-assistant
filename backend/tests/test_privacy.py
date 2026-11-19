"""Phase 5: privacy (GDPR). Finding, exporting and erasing one person's data; the nightly clean-up."""
import asyncio
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.jobs.handlers import schedule_daily
from app.jobs.runner import Jobs
from app.jobs.store import MemoryJobStore
from app.llm.base import ToolCall
from app.privacy.store import Person, PrivacyError, Retention
from tests.test_admin import staff
from tests.test_chat_api import NOW, say, start

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
pg = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to a Postgres with pgvector")


# ---------------------------------------------------------------- without a database
def test_retention_settings_are_checked():
    assert Retention.from_rules(None).conversations_days == 365
    assert Retention.from_rules({"conversations_days": 0}).conversations_days == 0      # 0 = keep
    for bad in ({"conversations_days": -1}, {"bookings_days": "a year"}, {"chats_days": 30},
                {"logs_days": True}):
        with pytest.raises(ValueError):
            Retention.from_rules(bad)


def test_a_person_is_an_email_or_a_full_phone_number():
    assert Person.parse(" Jo@Example.com ") == Person("jo@example.com", "")
    assert Person.parse(phone="+44 7700 900123").phone == Person.parse(phone="07700 900123").phone == "700900123"
    for email, phone in (("", ""), ("jo@", ""), ("", "1234")):
        with pytest.raises(PrivacyError):
            Person.parse(email, phone)
    assert "jo" not in Person.parse("jo@example.com").fingerprint


def test_a_bad_retention_edit_is_refused(make_client):
    client, app, _ = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    text = client.get("/api/admin/content/rules.yaml", headers=admin).json()["text"]
    assert "conversations_days: 365" in text
    r = client.put("/api/admin/content/rules.yaml", headers=admin,
                   json={"text": text.replace("conversations_days: 365", "conversations_days: -5"),
                         "base_version": 0})
    assert r.status_code == 422 and "privacy" in r.json()["detail"]
    assert client.post("/api/admin/privacy/find", headers=admin, json={"email": "a@b.co"}).status_code == 503


def test_the_clean_up_is_queued_once_a_day():
    jobs = Jobs(MemoryJobStore(), "manual")
    day = datetime(2030, 1, 7, 1, tzinfo=UTC)

    async def run():
        for hour in range(3):
            await schedule_daily(jobs, day + timedelta(hours=hour))
        await schedule_daily(jobs, day + timedelta(days=1))
        return list(jobs.store.jobs.values())
    queued = asyncio.run(run())
    assert sorted(j.key for j in queued) == ["privacy.retention:2030-01-07", "privacy.retention:2030-01-08"]


# ---------------------------------------------------------------- on Postgres
JO = [
    [ToolCall("a", "update_lead", {"need": "Move servers to Azure", "company_size": "about 45 people",
                                   "current_systems": "3 on-prem Windows servers"})],
    ["Thanks. What budget range do you have in mind?"],
    [ToolCall("b", "update_lead", {"budget": "20-30k", "timeline": "Q1", "name": "Jo Smith",
                                   "email": "Jo@Example.com", "phone": "+44 7700 900123"})],
    [ToolCall("c", "find_available_slots", {"date_from": "2030-01-08", "date_to": "2030-01-08",
                                            "preferred_time": "14:00"})],
    [ToolCall("d", "book_discovery_call", {"start": "2030-01-08T14:00:00+00:00"})],
    ["You're booked for Tuesday 8 January at 14:00."],
    [ToolCall("e", "update_lead", {"name": "Max Roe", "email": "max@example.com", "need": "Backups"})],
    ["Thanks Max."],
] + [["Summary."]] * 8


def run_jobs(client, app):
    for _ in range(5):
        if client.portal.call(app.state.worker.run_due) == 0:
            break


@pg
def test_find_export_and_erase_one_person(pg_env, make_client):
    client, app, _ = make_client(JO, now=NOW)
    jo, jo_headers, _ = start(client)
    say(client, jo, jo_headers, "We want to move our 3 servers to Azure, we are 45 people", "m-000001")
    say(client, jo, jo_headers, "Budget 20-30k, Q1. Jo Smith, jo@example.com, 07700 900123. Tuesday 2pm?",
        "m-000002")
    mx, mx_headers, _ = start(client)
    say(client, mx, mx_headers, "Max Roe, max@example.com, we need backups", "m-000003")
    admin = staff(client, app, "boss@acme.example", "admin")
    consultant = staff(client, app, "sam@acme.example", "consultant")
    assert client.post("/api/admin/privacy/find", headers=consultant, json={"email": "jo@example.com"}
                       ).status_code == 403

    # found by phone in another format; the upcoming call blocks erasure
    found = client.post("/api/admin/privacy/find", headers=admin, json={"phone": "07700 900123"}).json()
    assert [c["id"] for c in found["conversations"]] == [jo]
    booking = found["bookings"][0]
    assert booking["upcoming"] and found["blockers"]
    assert any("Google Calendar" in o for o in found["outside"])
    erase = {"email": "jo@example.com", "confirm": "jo@example.com"}
    refused = client.post("/api/admin/privacy/erase", headers=admin, json=erase)
    assert refused.status_code == 409 and booking["reference"] in refused.json()["detail"]
    assert client.post("/api/admin/privacy/erase", headers=admin,
                       json={**erase, "confirm": "max@example.com"}).status_code == 422

    # a copy of everything, for a subject access request
    copy = client.post("/api/admin/privacy/export", headers=admin, json={"email": "jo@example.com"}).json()
    assert copy["conversations"][0]["email"] == "jo@example.com"
    assert copy["conversations"][0]["messages"][0] == {**copy["conversations"][0]["messages"][0], "from": "you"}
    assert "Azure" in copy["conversations"][0]["messages"][0]["text"]
    assert copy["bookings"][0]["reference"] == booking["reference"]

    # cancel the call; once its calendar and email work is done, erase
    assert client.post(f"/api/admin/bookings/{booking['id']}/cancel", headers=admin).json()["status"] == "cancelled"
    assert "background task" in client.post("/api/admin/privacy/erase", headers=admin, json=erase).json()["detail"]
    run_jobs(client, app)
    done = client.post("/api/admin/privacy/erase", headers=admin, json=erase)
    assert done.status_code == 200, done.text
    result = done.json()
    assert result["counts"]["conversations"] == 1 and result["counts"]["bookings"] == 1
    assert client.post("/api/admin/privacy/find", headers=admin, json={"email": "jo@example.com"}
                       ).json()["conversations"] == []

    async def leftovers():
        conn = await asyncpg.connect(TEST_DB)
        try:
            rows = []
            for t in ("conversations", "messages", "leads", "bookings", "calls", "events", "jobs"):
                rows += [str(r) for r in await conn.fetch(f"SELECT * FROM {t}")]
            return " ".join(rows).lower(), await conn.fetchval(
                "SELECT count(*) FROM leads WHERE email = 'max@example.com'")
        finally:
            await conn.close()
    dump, max_kept = asyncio.run(leftovers())
    assert "jo@example.com" not in dump and "900123" not in dump and "jo smith" not in dump
    assert max_kept == 1                                                     # nobody else is touched
    log = client.get("/api/admin/privacy", headers=admin).json()
    kinds = [h["kind"] for h in log["history"]]
    assert kinds[:2] == ["privacy_erased", "privacy_exported"] and log["history"][0]["by"] == "Boss"
    assert log["history"][0]["data"]["person"] == Person.parse("jo@example.com").fingerprint


@pg
def test_old_records_are_deleted_by_the_nightly_clean_up(pg_env, make_client):
    client, app, _ = make_client([["Hello."], ["Hi again."]])
    old, h1, _ = start(client)
    say(client, old, h1, "Hello", "m-000001")
    new, h2, _ = start(client)
    say(client, new, h2, "Hello", "m-000002")

    async def age():
        conn = await asyncpg.connect(TEST_DB)
        await conn.execute("UPDATE conversations SET last_message_at = now() - interval '400 days' WHERE id = $1",
                           old)
        await conn.close()
    asyncio.run(age())
    admin = staff(client, app, "boss@acme.example", "admin")
    assert client.get("/api/admin/privacy", headers=admin).json()["retention"]["conversations_days"] == 365
    deleted = client.post("/api/admin/privacy/retention", headers=admin).json()["deleted"]
    assert deleted["conversations"] == 1
    ids = [c["id"] for c in client.get("/api/admin/conversations", headers=admin).json()["items"]]
    assert ids == [new]
    # the queued nightly job does the same, and records what it did
    client.portal.call(schedule_daily, app.state.jobs)
    run_jobs(client, app)
    history = client.get("/api/admin/privacy", headers=admin).json()["history"]
    assert [h["kind"] for h in history[:2]] == ["privacy_retention", "privacy_retention"]
    assert history[1]["by"] == "Boss" and history[0]["by"] == ""
