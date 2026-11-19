"""Availability, booking rules, no double booking, moving and cancelling, and the emails that follow."""
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.booking.emails import load_templates, render
from app.booking.service import BookingService, CalendarUnavailable, NotBookable
from app.booking.slots import BookingConfig, choose, describe, free_slots
from app.booking.store import AlreadyBooked, MemoryBookingStore, event_id_for, normalize_reference
from app.booking.team import Consultant, Team, load_team
from app.config import CONTENT_DIR
from app.jobs.runner import Jobs
from app.jobs.store import MemoryJobStore
from tests.fakes import FakeCalendar

LONDON, KARACHI = ZoneInfo("Europe/London"), ZoneInfo("Asia/Karachi")
CFG = BookingConfig()                                   # Mon-Fri 09:00-17:30 London, 30 min, 15 min buffer
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=UTC)           # Monday 10:00 London
TUE_9 = datetime(2030, 1, 8, 9, 0, tzinfo=UTC)


def at(h: int, m: int = 0, day: int = 8) -> datetime:
    return datetime(2030, 1, day, h, m, tzinfo=UTC)


# ---------------- slots ----------------
def test_slots_respect_hours_notice_and_weekends():
    slots = free_slots(CFG, now=NOW, busy=[], window_start=NOW, window_end=NOW + timedelta(days=7))
    assert slots[0] == TUE_9                              # 12 hours' notice: nothing left on Monday
    assert all(s.astimezone(LONDON).weekday() < 5 for s in slots)
    assert all(time(9) <= s.astimezone(LONDON).time() <= time(17) for s in slots)
    assert at(17, 0) in slots and at(17, 30) not in slots   # the last call ends at 17:30
    assert len([s for s in slots if s.date() == date(2030, 1, 8)]) == 17


def test_slots_keep_a_buffer_around_busy_times():
    busy = [(at(11), at(12))]
    slots = free_slots(CFG, now=NOW, busy=busy, window_start=at(9), window_end=at(17, 30))
    assert at(10) in slots                     # ends 10:30, 30 min before the meeting
    assert at(10, 30) not in slots             # would end at 11:00 with no buffer
    assert at(11) not in slots and at(12) not in slots   # 12:00 starts right after: no buffer either
    assert at(12, 30) in slots


def test_slots_follow_daylight_saving():
    # UK clocks go forward on 31 March 2030: 09:00 London is 08:00 UTC from then on
    now = datetime(2030, 3, 29, 8, 0, tzinfo=UTC)
    slots = free_slots(CFG, now=now, busy=[], window_start=now, window_end=now + timedelta(days=5))
    first_april = [s for s in slots if s.astimezone(LONDON).date() == date(2030, 4, 1)][0]
    assert first_april == datetime(2030, 4, 1, 8, 0, tzinfo=UTC)


def test_choice_spreads_over_days_or_follows_a_preference():
    slots = free_slots(CFG, now=NOW, busy=[], window_start=NOW, window_end=NOW + timedelta(days=7))
    spread = choose(slots, visitor_tz=LONDON, preferred=None, limit=6)
    assert len({s.date() for s in spread}) == 3 and len(spread) == 6
    near_two = choose(slots, visitor_tz=LONDON, preferred=time(14), limit=6)
    assert near_two[0] == at(13, 30) and at(14) in near_two


def test_times_are_described_in_the_visitors_zone():
    assert describe(at(9), at(9, 30), KARACHI) == "Tuesday 8 January, 14:00-14:30 (Karachi time)"
    assert describe(at(9), at(9, 30), ZoneInfo("UTC")) == "Tuesday 8 January, 09:00-09:30 (UTC)"


def test_config_and_team_load_from_content():
    from app.kb.content import load_rules
    cfg = BookingConfig.from_rules(load_rules(CONTENT_DIR).booking)
    assert cfg.days == (0, 1, 2, 3, 4) and cfg.end == time(17, 30) and cfg.reminders[0] == ("reminder_24h", 1440)
    team = load_team(CONTENT_DIR)
    assert team.default.id == "sam" and team.for_handover("security_incident").id == "sam"
    with pytest.raises(ValueError):
        Team([Consultant("a", "A"), Consultant("a", "B")])


def test_routing_by_need():
    team = Team([Consultant("gen", "Gen", default=True, areas=["cloud"]),
                 Consultant("sec", "Sec", calendar_id="sec@x", areas=["security", "iso 27001"])])
    assert team.for_need("We need an ISO 27001 security review").id == "sec"
    assert team.for_need("Something else").id == "gen"


# ---------------- booking service ----------------
def service(calendar=None, team=None):
    jobs = Jobs(MemoryJobStore(), "manual")
    team = team or Team([Consultant("sam", "Sam Patel", default=True)])
    return BookingService(MemoryBookingStore(), calendar or FakeCalendar(), team, CFG, jobs), jobs


def book(svc, conv="c1", start=TUE_9, email="jo@example.com"):
    return asyncio.run(svc.book(conversation_id=conv, consultant=svc.team.default, start=start, visitor_tz=LONDON,
                                name="Jo Smith", email=email, now=NOW))


def test_booking_queues_everything_that_follows():
    svc, jobs = service()
    b, created = book(svc)
    assert created and b.reference.startswith("DC-") and b.calendar_event_id == event_id_for(b.id)
    kinds = sorted(j.kind + ":" + j.payload.get("template", "") for j in jobs.store.jobs.values())
    assert kinds == ["calendar.create:", "conversation.summarize:", "email.booking:confirmation",
                     "email.booking:reminder_1h", "email.booking:reminder_24h"]
    reminder = next(j for j in jobs.store.jobs.values() if j.payload.get("template") == "reminder_24h")
    assert reminder.run_at == TUE_9 - timedelta(hours=24)


def test_a_retried_booking_returns_the_same_call():
    svc, jobs = service()
    b1, _ = book(svc)
    b2, created = book(svc)
    assert not created and b2.id == b1.id and len(jobs.store.jobs) == 5


def test_no_double_booking_and_one_call_per_conversation():
    svc, _ = service()
    book(svc, conv="c1")
    with pytest.raises(NotBookable):           # same time, another visitor
        book(svc, conv="c2")
    with pytest.raises(NotBookable):           # overlapping buffer
        book(svc, conv="c2", start=TUE_9 + timedelta(minutes=30))
    book(svc, conv="c2", start=TUE_9 + timedelta(minutes=60))
    with pytest.raises(AlreadyBooked):         # a second, different time in the same conversation
        book(svc, conv="c1", start=at(15))


def test_outside_busy_times_and_bad_times_are_refused():
    cal = FakeCalendar()
    cal.busy_times = [(at(9), at(10))]
    svc, _ = service(cal)
    with pytest.raises(NotBookable):
        book(svc)                                       # busy in the consultant's calendar
    with pytest.raises(NotBookable):
        book(svc, start=at(9, 10, day=9))               # not on the slot grid
    with pytest.raises(NotBookable):
        book(svc, start=datetime(2030, 1, 12, 10, tzinfo=UTC))   # Saturday
    with pytest.raises(NotBookable):
        book(svc, start=NOW + timedelta(hours=2))       # less than 12 hours' notice


def test_calendar_down_is_reported_not_guessed():
    cal = FakeCalendar()
    cal.down = True
    svc, _ = service(cal)
    with pytest.raises(CalendarUnavailable):
        book(svc)


def test_reschedule_replaces_reminders_and_cancel_stops_them():
    svc, jobs = service()
    b, _ = book(svc)
    moved = asyncio.run(svc.reschedule(b, at(15), now=NOW))
    assert moved.start_at == at(15)
    reminders = [j for j in jobs.store.jobs.values() if j.payload.get("template", "").startswith("reminder")]
    old = [j for j in reminders if j.payload["start"] == TUE_9.isoformat()]
    new = [j for j in reminders if j.payload["start"] == at(15).isoformat()]
    assert {j.status for j in old} == {"cancelled"} and {j.status for j in new} == {"pending"}
    assert any(j.kind == "calendar.move" for j in jobs.store.jobs.values())
    # the old time is free again for someone else
    book(svc, conv="c2", start=TUE_9)
    asyncio.run(svc.cancel(moved))
    assert {j.status for j in jobs.store.jobs.values() if j.ref == b.id and j.kind == "email.booking"
            and j.payload["template"].startswith("reminder")} == {"cancelled"}
    assert any(j.payload.get("template") == "cancelled" for j in jobs.store.jobs.values())


# ---------------- emails ----------------
def test_email_templates_render_in_the_visitors_time_zone():
    svc, _ = service()
    b, _ = book(svc)
    b.visitor_timezone = "Asia/Karachi"
    templates = load_templates(CONTENT_DIR)
    subject, body = render(templates["confirmation"], booking=b, consultant=svc.team.default, cfg=CFG,
                           firm_name="Acme IT Consulting")
    assert subject == "Your discovery call with Acme IT Consulting: Tuesday 8 January, 14:00-14:30 (Karachi time)"
    assert "Hi Jo," in body and b.reference in body and "Sam Patel" in body and "{" not in body
    assert "Video link: in the calendar invitation" in body          # before the Meet link exists
    b.meeting_url = "https://meet.google.com/abc-defg-hij"
    _, body = render(templates["reminder_1h"], booking=b, consultant=svc.team.default, cfg=CFG, firm_name="Acme")
    assert "Video link: https://meet.google.com/abc-defg-hij" in body


def test_references_are_found_however_they_are_typed_back():
    for typed in ("DC-7K3M9Q", "dc\u20117k3m9q", " DC 7K3M9Q ", "7k3m9q", "DC\u20137K3M9Q"):
        assert normalize_reference(typed) == "DC-7K3M9Q"
    svc, _ = service()
    b, _ = book(svc)
    assert asyncio.run(svc.store.by_reference(b.reference.lower().replace("-", "\u2011"))).id == b.id


def test_the_model_gets_a_calendar_instead_of_doing_date_arithmetic():
    from app.assistant.prompts import calendar_lines
    text = calendar_lines(datetime(2026, 11, 17, 14, 0, tzinfo=UTC), KARACHI)      # Tuesday
    lines = text.splitlines()
    assert lines[1].startswith("This week: Tue 17 Nov=2026-11-17 (today), Wed 18 Nov=2026-11-18")
    assert "Mon 16 Nov" not in text                                  # past days are left out
    assert lines[2].startswith("Next week: Mon 23 Nov=2026-11-23") and "Wed 25 Nov=2026-11-25" in lines[2]
    assert lines[3].startswith("The week after: Mon 30 Nov=2026-11-30")
    # late evening in Karachi is already the next day there
    assert "(today)" in calendar_lines(datetime(2026, 11, 17, 20, 0, tzinfo=UTC), KARACHI).split("Wed 18 Nov")[1][:20]
