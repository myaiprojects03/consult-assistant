"""Bookings and jobs on real Postgres: the database itself prevents double booking and double running.
Skipped without TEST_DATABASE_URL."""
import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.booking.service import BookingService, NotBookable
from app.booking.slots import BookingConfig
from app.booking.store import AlreadyBooked, Booking, PgBookingStore, SlotTaken
from app.booking.team import Consultant, Team
from app.db.repo import Lead
from app.jobs.runner import Jobs
from app.jobs.store import Job, PgJobStore
from tests.fakes import FakeCalendar
from tests.test_content_and_storage import pg, with_repo

NOW = datetime(2030, 1, 7, 10, 0, tzinfo=UTC)
TUE_9 = datetime(2030, 1, 8, 9, 0, tzinfo=UTC)


def booking(conv, start=TUE_9, minutes=30, calendar="primary"):
    return Booking(id="", reference="", conversation_id=conv, consultant_id="sam", calendar_id=calendar,
                   start_at=start, end_at=start + timedelta(minutes=minutes), name="Jo", email="jo@example.com")


@pg
def test_database_rejects_overlapping_calls_and_a_second_call_per_conversation():
    async def check(repo):
        store = PgBookingStore(repo.pool)
        c1 = (await repo.create_conversation("web", "h", "")).id
        c2 = (await repo.create_conversation("web", "h", "")).id
        b = await store.create(booking(c1))
        assert b.reference.startswith("DC-") and b.calendar_event_id == "dc" + b.id.replace("-", "")
        with pytest.raises(SlotTaken):
            await store.create(booking(c2, TUE_9 + timedelta(minutes=15)))
        await store.create(booking(c2, TUE_9 + timedelta(minutes=30)))       # touching is fine
        with pytest.raises(AlreadyBooked):
            await store.create(booking(c1, TUE_9 + timedelta(hours=3)))
        await store.create(booking(None, TUE_9, calendar="other@x"))          # another calendar
        await store.set_meeting_url(b.id, "https://meet.google.com/x")
        assert (await store.get(b.id)).meeting_url == "https://meet.google.com/x"
        moved = await store.move(b.id, TUE_9 + timedelta(hours=2), TUE_9 + timedelta(hours=2, minutes=30))
        assert moved.start_at == TUE_9 + timedelta(hours=2)
        assert await store.busy("primary", TUE_9, TUE_9 + timedelta(hours=1)) == [
            (TUE_9 + timedelta(minutes=30), TUE_9 + timedelta(minutes=60))]
        await store.cancel(b.id)
        assert await store.active_for_conversation(c1) is None
        assert (await store.by_reference(b.reference.lower())).status == "cancelled"
    with_repo(check)


@pg
def test_two_visitors_racing_for_one_slot_get_one_booking():
    async def check(repo):
        convs = [(await repo.create_conversation("web", "h", "")).id for _ in range(5)]
        jobs = Jobs(PgJobStore(repo.pool), "manual")
        svc = BookingService(PgBookingStore(repo.pool), FakeCalendar(), Team([Consultant("sam", "Sam", default=True)]),
                             BookingConfig(), jobs)

        async def attempt(conv):
            try:
                await svc.book(conversation_id=conv, consultant=svc.team.default, start=TUE_9,
                               visitor_tz=ZoneInfo("Europe/London"), name="Jo", email="jo@example.com", now=NOW)
                return "booked"
            except NotBookable:
                return "taken"
        results = await asyncio.gather(*(attempt(c) for c in convs))
        assert sorted(results) == ["booked", "taken", "taken", "taken", "taken"]
        counts = await jobs.store.counts()
        assert counts == {"pending": 5}       # the jobs of exactly one booking
    with_repo(check)


@pg
def test_jobs_claim_once_and_idempotent_keys():
    async def check(repo):
        store = PgJobStore(repo.pool)
        for i in range(10):
            await store.add(Job(id="", kind="t", key=f"k{i}", payload={"i": i}, ref="r"))
        again, created = await store.add(Job(id="", kind="t", key="k3", payload={"i": 99}))
        assert not created and again.payload == {"i": 3}
        same, created = await store.add(Job(id="", kind="t", key="k-new", ref="r"), coalesce=True)
        assert not created                                   # a pending job about "r" exists
        claimed = await asyncio.gather(*(store.claim() for _ in range(12)))
        ids = [j.id for j in claimed if j]
        assert len(ids) == 10 and len(set(ids)) == 10       # every job once, none twice
        await store.retry(ids[0], "boom", datetime.now(UTC) - timedelta(seconds=1))
        j = await store.claim(ids[0])
        assert j is not None and j.attempts == 2 and j.last_error == "boom"
        await store.finish(ids[0])
        reminder, _ = await store.add(Job(id="", kind="email.booking", key="rem", ref="b1",
                                          run_at=datetime.now(UTC) + timedelta(days=1)))
        assert await store.cancel("b1", ["email.booking"]) == 1
        assert reminder.id not in [x.id for x in await store.undispatched(datetime.now(UTC) + timedelta(days=2))]
        counts = await store.counts()
        assert counts["done"] == 1 and counts["running"] == 9 and counts["cancelled"] == 1
    with_repo(check)


@pg
def test_conversation_summary_timezone_and_crm_ids():
    async def check(repo):
        conv = await repo.create_conversation("web", "h", "", "Asia/Karachi")
        await repo.set_summary(conv.id, "Wants Azure.")
        got = await repo.get_conversation(conv.id)
        assert got.visitor_timezone == "Asia/Karachi" and got.summary == "Wants Azure."
        await repo.escalate(conv.id, "security_incident", "Ransomware.")
        got = await repo.get_conversation(conv.id)
        assert (got.escalation_reason, got.escalation_summary, got.summary) == ("security_incident", "Ransomware.",
                                                                               "Wants Azure.")
        await repo.save_lead(Lead(conv.id, email="jo@example.com"))
        await repo.save_crm_ids(conv.id, "101", "201", "301")
        await repo.save_lead(Lead(conv.id, email="jo@example.com", name="Jo"))     # a later save keeps the ids
        lead = await repo.get_lead(conv.id)
        assert (lead.crm_contact_id, lead.crm_deal_id, lead.crm_note_id, lead.name) == ("101", "201", "301", "Jo")
    with_repo(check)
