"""Booking, moving and cancelling discovery calls.

Availability = the office calendar (slots.py) minus the consultant's busy times in the outside calendar
minus confirmed bookings in our database. A requested time is checked again at the moment of booking,
and the database constraint is the final word on overlaps. Everything that follows a booking (calendar
event and invitation, confirmation email, reminders, CRM, Slack) is queued as background jobs, so the
visitor gets an answer at once and nothing is lost if a service is briefly down.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.booking.slots import BookingConfig, choose, day_window, describe, free_slots
from app.booking.store import AlreadyBooked, Booking, BookingStore, SlotTaken
from app.booking.team import Consultant, Team
from app.integrations.base import Calendar, IntegrationError, Interval
from app.jobs.runner import Jobs

REMINDER_KINDS = ["email.booking", "sms.booking"]
SMS_REMINDERS = ("reminder_24h",)        # text messages: one reminder is enough


class CalendarUnavailable(Exception):  # noqa: N818
    """The outside calendar could not be read, so availability cannot be promised."""


class NotBookable(Exception):  # noqa: N818
    """The requested time is not an offered slot (outside hours, too soon, or taken)."""


@dataclass
class Offer:
    start: datetime
    end: datetime
    label: str


class BookingService:
    def __init__(self, store: BookingStore, calendar: Calendar, team: Team, cfg: BookingConfig, jobs: Jobs):
        self.store, self.calendar, self.team, self.cfg, self.jobs = store, calendar, team, cfg, jobs

    # ---------------------------------------------------------------- availability
    async def _busy(self, consultant: Consultant, start: datetime, end: datetime,
                    exclude_id: str | None = None) -> list[Interval]:
        pad = timedelta(minutes=self.cfg.buffer_minutes)
        try:
            outside = await self.calendar.busy([consultant.calendar_id], start - pad, end + pad)
        except IntegrationError as exc:
            raise CalendarUnavailable(str(exc)) from exc
        return outside.get(consultant.calendar_id, []) + await self.store.busy(
            consultant.calendar_id, start - pad, end + pad, exclude_id)

    async def offers(self, *, consultant: Consultant, visitor_tz: ZoneInfo, day_from: date | None = None,
                     day_to: date | None = None, preferred: time | None = None,
                     exclude_id: str | None = None, now: datetime | None = None) -> list[Offer]:
        now = now or datetime.now(UTC)
        today = now.astimezone(visitor_tz).date()
        day_from = max(day_from or today, today)
        horizon = today + timedelta(days=self.cfg.days_ahead)
        day_to = min(day_to or horizon, horizon)
        if day_to < day_from:
            return []
        start, end = day_window(day_from, day_to, visitor_tz)
        busy = await self._busy(consultant, start, end, exclude_id)
        slots = free_slots(self.cfg, now=now, busy=busy, window_start=start, window_end=end)
        picked = choose(slots, visitor_tz=visitor_tz, preferred=preferred, limit=self.cfg.max_offered)
        return [Offer(s, s + self.cfg.slot, describe(s, s + self.cfg.slot, visitor_tz)) for s in picked]

    async def _check_free(self, consultant: Consultant, start: datetime, exclude_id: str | None,
                          now: datetime | None) -> datetime:
        start = start.astimezone(UTC)
        busy = await self._busy(consultant, start, start + self.cfg.slot, exclude_id)
        ok = free_slots(self.cfg, now=now or datetime.now(UTC), busy=busy, window_start=start,
                        window_end=start + self.cfg.slot)
        if not any(s == start for s in ok):
            raise NotBookable()
        return start

    # ---------------------------------------------------------------- changes
    async def book(self, *, conversation_id: str | None, consultant: Consultant, start: datetime,
                   visitor_tz: ZoneInfo, name: str, email: str, phone: str = "", notes: str = "",
                   now: datetime | None = None) -> tuple[Booking, bool]:
        """Returns (booking, created). A retry of the same request returns the existing booking."""
        if conversation_id:
            existing = await self.store.active_for_conversation(conversation_id)
            if existing:
                if existing.start_at == start.astimezone(UTC):
                    return existing, False
                raise AlreadyBooked(existing)
        start = await self._check_free(consultant, start, None, now)
        try:
            b = await self.store.create(Booking(
                id="", reference="", conversation_id=conversation_id, consultant_id=consultant.id,
                calendar_id=consultant.calendar_id, start_at=start, end_at=start + self.cfg.slot, name=name,
                email=email, phone=phone, visitor_timezone=visitor_tz.key, notes=notes))
        except SlotTaken as exc:      # someone confirmed an overlapping time a moment earlier
            raise NotBookable() from exc
        await self._after_change(b, "booked")
        return b, True

    async def reschedule(self, booking: Booking, new_start: datetime, now: datetime | None = None) -> Booking:
        consultant = self.team.get(booking.consultant_id)
        if new_start.astimezone(UTC) == booking.start_at:
            return booking
        start = await self._check_free(consultant, new_start, booking.id, now)
        try:
            moved = await self.store.move(booking.id, start, start + self.cfg.slot)
        except SlotTaken as exc:
            raise NotBookable() from exc
        await self.jobs.store.cancel(booking.id, REMINDER_KINDS)
        await self._after_change(moved, "rescheduled")
        return moved

    async def cancel(self, booking: Booking) -> Booking:
        cancelled = await self.store.cancel(booking.id)
        await self.jobs.store.cancel(booking.id, REMINDER_KINDS)
        await self._after_change(cancelled, "cancelled")
        return cancelled

    async def _after_change(self, b: Booking, change: str) -> None:
        stamp = int(b.start_at.timestamp())
        version = f"{b.id}:{change}:{stamp}"
        payload = {"booking_id": b.id, "start": b.start_at.isoformat()}
        if change == "booked":
            await self.jobs.enqueue("calendar.create", f"cal:{version}", payload, ref=b.id)
        elif change == "rescheduled":
            await self.jobs.enqueue("calendar.move", f"cal:{version}", payload, ref=b.id)
        else:
            await self.jobs.enqueue("calendar.cancel", f"cal:{version}", payload, ref=b.id)
        template = {"booked": "confirmation", "rescheduled": "rescheduled", "cancelled": "cancelled"}[change]
        await self.jobs.enqueue("email.booking", f"email:{template}:{version}",
                                {**payload, "template": template}, ref=b.id)
        if b.phone:
            await self.jobs.enqueue("sms.booking", f"sms:{template}:{version}", {**payload, "template": template},
                                    ref=b.id)
        if change != "cancelled":
            now = datetime.now(UTC)
            for template_name, minutes in self.cfg.reminders:
                when = b.start_at - timedelta(minutes=minutes)
                if when > now + timedelta(minutes=5):
                    await self.jobs.enqueue("email.booking", f"email:{template_name}:{version}",
                                            {**payload, "template": template_name}, ref=b.id, run_at=when)
                    if b.phone and template_name in SMS_REMINDERS:
                        await self.jobs.enqueue("sms.booking", f"sms:{template_name}:{version}",
                                                {**payload, "template": template_name}, ref=b.id, run_at=when)
        if b.conversation_id:
            # summary first, so the Slack message and the CRM note carry it
            await self.jobs.enqueue("conversation.summarize", f"summary:{version}",
                                    {"conversation_id": b.conversation_id, "purpose": "booking",
                                     "booking_id": b.id, "change": change}, ref=b.conversation_id)
        else:
            await self.jobs.enqueue("slack.booking", f"slack:{version}", {"booking_id": b.id, "change": change},
                                    ref=b.id)
