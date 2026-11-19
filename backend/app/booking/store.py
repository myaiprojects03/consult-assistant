"""Bookings. The database is the source of truth for who has which time; the outside calendar mirrors it.

Double booking is impossible at the database level: an exclusion constraint rejects two confirmed calls
that overlap on the same calendar, even if two visitors confirm the same time in the same instant, and
a unique index allows one active call per conversation.
"""
from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

import asyncpg

REF_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # no 0/O or 1/I mix-ups when read out on a call


def new_reference() -> str:
    return "DC-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(6))


DASHES = "\u2010\u2011\u2012\u2013\u2014\u2212"   # models and phones often turn "-" into these


def normalize_reference(text: str) -> str:
    """'dc\u20117k3m9q', ' DC 7K3M9Q ', '7K3M9Q' -> 'DC-7K3M9Q' (as typed back by visitors or rewritten by models)."""
    ref = "".join("-" if ch in DASHES else ch for ch in text).upper().replace(" ", "")
    ref = ref.removeprefix("DC-").removeprefix("DC")
    return f"DC-{ref}"


def event_id_for(booking_id: str) -> str:
    """Google Calendar event ids allow a-v and 0-9; a UUID's hex digits fit."""
    return "dc" + booking_id.replace("-", "")


@dataclass
class Booking:
    id: str
    reference: str
    conversation_id: str | None
    consultant_id: str
    calendar_id: str
    start_at: datetime
    end_at: datetime
    name: str
    email: str
    phone: str = ""
    visitor_timezone: str = ""
    notes: str = ""
    status: str = "confirmed"
    calendar_event_id: str = ""
    meeting_url: str = ""


class SlotTaken(Exception):  # noqa: N818
    """Someone else holds an overlapping confirmed call on that calendar."""


class AlreadyBooked(Exception):  # noqa: N818
    """This conversation already has a confirmed call."""

    def __init__(self, existing: Booking):
        super().__init__(existing.reference)
        self.existing = existing


class BookingStore(ABC):
    @abstractmethod
    async def create(self, b: Booking) -> Booking: ...
    @abstractmethod
    async def get(self, booking_id: str) -> Booking | None: ...
    @abstractmethod
    async def by_reference(self, reference: str) -> Booking | None: ...
    @abstractmethod
    async def active_for_conversation(self, conversation_id: str) -> Booking | None: ...
    @abstractmethod
    async def busy(self, calendar_id: str, start: datetime, end: datetime,
                   exclude_id: str | None = None) -> list[tuple[datetime, datetime]]: ...
    @abstractmethod
    async def move(self, booking_id: str, start: datetime, end: datetime) -> Booking: ...
    @abstractmethod
    async def cancel(self, booking_id: str) -> Booking: ...
    @abstractmethod
    async def set_meeting_url(self, booking_id: str, url: str) -> None: ...


def _row(r: asyncpg.Record) -> Booking:
    return Booking(id=str(r["id"]), reference=r["reference"],
                   conversation_id=str(r["conversation_id"]) if r["conversation_id"] else None,
                   consultant_id=r["consultant_id"], calendar_id=r["calendar_id"], start_at=r["start_at"],
                   end_at=r["end_at"], name=r["name"], email=r["email"], phone=r["phone"],
                   visitor_timezone=r["visitor_timezone"], notes=r["notes"], status=r["status"],
                   calendar_event_id=r["calendar_event_id"], meeting_url=r["meeting_url"])


class PgBookingStore(BookingStore):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, b: Booking) -> Booking:
        bid = str(uuid4())
        for _ in range(3):   # a reference collision is astronomically rare; try a new one
            try:
                r = await self.pool.fetchrow(
                    "INSERT INTO bookings (id, reference, conversation_id, consultant_id, calendar_id, start_at, "
                    "end_at, name, email, phone, visitor_timezone, notes, calendar_event_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *",
                    UUID(bid), b.reference or new_reference(), UUID(b.conversation_id) if b.conversation_id else None,
                    b.consultant_id, b.calendar_id, b.start_at, b.end_at, b.name, b.email, b.phone,
                    b.visitor_timezone, b.notes, event_id_for(bid))
                return _row(r)
            except asyncpg.exceptions.ExclusionViolationError as exc:
                raise SlotTaken() from exc
            except asyncpg.exceptions.UniqueViolationError as exc:
                if "one_per_conversation" in str(exc.constraint_name) and b.conversation_id:
                    existing = await self.active_for_conversation(b.conversation_id)
                    if existing:
                        raise AlreadyBooked(existing) from exc
                b = replace(b, reference=new_reference())
        raise RuntimeError("could not create a unique booking reference")

    async def get(self, booking_id: str) -> Booking | None:
        try:
            r = await self.pool.fetchrow("SELECT * FROM bookings WHERE id = $1", UUID(booking_id))
        except ValueError:
            return None
        return _row(r) if r else None

    async def by_reference(self, reference: str) -> Booking | None:
        r = await self.pool.fetchrow("SELECT * FROM bookings WHERE reference = $1", normalize_reference(reference))
        return _row(r) if r else None

    async def active_for_conversation(self, conversation_id: str) -> Booking | None:
        r = await self.pool.fetchrow("SELECT * FROM bookings WHERE conversation_id = $1 AND status = 'confirmed'",
                                     UUID(conversation_id))
        return _row(r) if r else None

    async def busy(self, calendar_id: str, start: datetime, end: datetime,
                   exclude_id: str | None = None) -> list[tuple[datetime, datetime]]:
        rows = await self.pool.fetch(
            "SELECT start_at, end_at FROM bookings WHERE calendar_id = $1 AND status = 'confirmed' "
            "AND start_at < $3 AND end_at > $2 AND ($4::uuid IS NULL OR id <> $4::uuid)",
            calendar_id, start, end, UUID(exclude_id) if exclude_id else None)
        return [(r["start_at"], r["end_at"]) for r in rows]

    async def move(self, booking_id: str, start: datetime, end: datetime) -> Booking:
        try:
            r = await self.pool.fetchrow("UPDATE bookings SET start_at = $2, end_at = $3, updated_at = now() "
                                         "WHERE id = $1 AND status = 'confirmed' RETURNING *",
                                         UUID(booking_id), start, end)
        except asyncpg.exceptions.ExclusionViolationError as exc:
            raise SlotTaken() from exc
        if r is None:
            raise LookupError("booking is not active")
        return _row(r)

    async def cancel(self, booking_id: str) -> Booking:
        r = await self.pool.fetchrow("UPDATE bookings SET status = 'cancelled', cancelled_at = now(), "
                                     "updated_at = now() WHERE id = $1 RETURNING *", UUID(booking_id))
        if r is None:
            raise LookupError("booking not found")
        return _row(r)

    async def set_meeting_url(self, booking_id: str, url: str) -> None:
        await self.pool.execute("UPDATE bookings SET meeting_url = $2 WHERE id = $1", UUID(booking_id), url)


class MemoryBookingStore(BookingStore):
    def __init__(self) -> None:
        self.bookings: dict[str, Booking] = {}

    def _clash(self, calendar_id: str, start: datetime, end: datetime, exclude: str | None) -> bool:
        return any(b.status == "confirmed" and b.calendar_id == calendar_id and b.id != exclude
                   and b.start_at < end and start < b.end_at for b in self.bookings.values())

    async def create(self, b: Booking) -> Booking:
        if self._clash(b.calendar_id, b.start_at, b.end_at, None):
            raise SlotTaken()
        if b.conversation_id:
            existing = await self.active_for_conversation(b.conversation_id)
            if existing:
                raise AlreadyBooked(existing)
        bid = str(uuid4())
        stored = replace(b, id=bid, reference=b.reference or new_reference(), calendar_event_id=event_id_for(bid))
        self.bookings[bid] = stored
        return replace(stored)

    async def get(self, booking_id: str) -> Booking | None:
        b = self.bookings.get(booking_id)
        return replace(b) if b else None

    async def by_reference(self, reference: str) -> Booking | None:
        ref = normalize_reference(reference)
        return next((replace(b) for b in self.bookings.values() if b.reference == ref), None)

    async def active_for_conversation(self, conversation_id: str) -> Booking | None:
        return next((replace(b) for b in self.bookings.values()
                     if b.conversation_id == conversation_id and b.status == "confirmed"), None)

    async def busy(self, calendar_id: str, start: datetime, end: datetime,
                   exclude_id: str | None = None) -> list[tuple[datetime, datetime]]:
        return [(b.start_at, b.end_at) for b in self.bookings.values()
                if b.status == "confirmed" and b.calendar_id == calendar_id and b.id != exclude_id
                and b.start_at < end and b.end_at > start]

    async def move(self, booking_id: str, start: datetime, end: datetime) -> Booking:
        b = self.bookings[booking_id]
        if b.status != "confirmed":
            raise LookupError("booking is not active")
        if self._clash(b.calendar_id, start, end, booking_id):
            raise SlotTaken()
        b.start_at, b.end_at = start, end
        return replace(b)

    async def cancel(self, booking_id: str) -> Booking:
        b = self.bookings[booking_id]
        b.status = "cancelled"
        return replace(b)

    async def set_meeting_url(self, booking_id: str, url: str) -> None:
        self.bookings[booking_id].meeting_url = url
