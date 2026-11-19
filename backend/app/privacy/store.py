"""Privacy (GDPR): find, export and erase one person's data, and delete old records every night.

A person is found by email and/or phone number across conversations (chat, phone and browser calls),
leads, call requests, bookings, calls and tickets. Conversations of the internal assistant (channel
'team') are staff work and are not searched; they follow the retention periods like everything else.

What erasure does:
- deletes the person's conversations (transcripts, lead details, call requests and their log entries),
  bookings, calls, background tasks about them and log entries that mention them (an upcoming booking
  must be cancelled first, and its calendar and email updates must have run);
- keeps tickets (a client company's support record) but removes the contact email from them;
- keeps a client contact entry (it is how a client's staff sign in) and says so;
- lists records in outside systems (HubSpot, Google Calendar, Jira, Slack) that staff remove there;
- records the request in the audit log with a one-way hash of the email or phone, never the value.

Retention periods come from `privacy:` in content/rules.yaml (editable in the dashboard). 0 keeps for ever.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

PHONE_DIGITS = 9            # numbers match on their last 9 digits: +44 7700 900123 = 07700 900123
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PrivacyError(ValueError):
    """The request cannot be carried out as asked; the message says why, for staff."""


@dataclass(frozen=True)
class Retention:
    conversations_days: int = 365     # chats and calls: transcripts, lead details, call requests
    bookings_days: int = 730          # discovery calls, counted from the call's end
    logs_days: int = 365              # audit log entries (privacy requests themselves are kept)
    finished_jobs_days: int = 90      # completed background tasks (they hold names and emails)

    @classmethod
    def from_rules(cls, data: dict[str, Any] | None) -> Retention:
        data = data or {}
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"privacy: unknown setting(s) {', '.join(sorted(unknown))}")
        values: dict[str, int] = {}
        for name, value in data.items():
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3650:
                raise ValueError(f"privacy.{name} must be a whole number of days from 0 to 3650 (0 = keep)")
            values[name] = value
        return cls(**values)

    def describe(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class Person:
    email: str = ""
    phone: str = ""                   # last PHONE_DIGITS digits

    @classmethod
    def parse(cls, email: str = "", phone: str = "") -> Person:
        email = email.strip().lower()
        digits = re.sub(r"\D", "", phone)
        if email and not EMAIL.match(email):
            raise PrivacyError("That email address does not look complete.")
        if phone.strip() and len(digits) < PHONE_DIGITS:
            raise PrivacyError("Enter the full phone number, with area code.")
        if not email and not digits:
            raise PrivacyError("Enter an email address or a phone number.")
        return cls(email, digits[-PHONE_DIGITS:] if digits else "")

    @property
    def fingerprint(self) -> str:
        """Stored in the audit log instead of the email or number: proves which request was handled."""
        return hashlib.sha256(f"{self.email}|{self.phone}".encode()).hexdigest()[:16]


# $1 = email (lower case, '' = not given), $2 = phone digits ('' = not given)
def _email(col: str) -> str:
    return f"($1::text <> '' AND lower(trim({col})) = $1::text)"


def _phone(col: str, n: int = 2) -> str:
    return f"(${n}::text <> '' AND right(regexp_replace({col}, '\\D', '', 'g'), {PHONE_DIGITS}) = ${n}::text)"


CONVERSATIONS = f"""
    SELECT c.id FROM conversations c WHERE c.channel <> 'team' AND c.id IN (
        SELECT conversation_id FROM leads WHERE {_email('email')} OR {_phone('phone')}
        UNION SELECT id FROM conversations WHERE {_email('verified_email')}
        UNION SELECT conversation_id FROM bookings
              WHERE conversation_id IS NOT NULL AND ({_email('email')} OR {_phone('phone')})
        UNION SELECT conversation_id FROM calls
              WHERE conversation_id IS NOT NULL AND ({_phone('from_number')} OR {_phone('to_number')})
        UNION SELECT conversation_id FROM tickets WHERE conversation_id IS NOT NULL AND {_email('contact_email')}
        UNION SELECT conversation_id FROM messages
              WHERE role = 'user' AND $1::text <> '' AND position($1::text in lower(text)) > 0)
"""
BOOKINGS = f"""SELECT * FROM bookings WHERE conversation_id = ANY($3::uuid[])
               OR {_email('email')} OR {_phone('phone')} ORDER BY start_at"""
CALLS = f"""SELECT * FROM calls WHERE conversation_id = ANY($2::uuid[])           -- $1 = phone digits
            OR {_phone('from_number', 1)} OR {_phone('to_number', 1)} ORDER BY created_at"""
TICKETS = f"""SELECT t.*, cl.name AS client_name FROM tickets t JOIN clients cl ON cl.id = t.client_id
              WHERE t.conversation_id = ANY($2::uuid[]) OR {_email('t.contact_email')} ORDER BY t.created_at"""
CONTACTS = f"""SELECT cc.name, cc.role, cl.name AS client_name FROM client_contacts cc
               JOIN clients cl ON cl.id = cc.client_id WHERE {_email('cc.email')}"""


def _plain(rows: list[asyncpg.Record], keep: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = {k: r[k] for k in keep if k in r.keys()}
        out.append({k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v)
                    for k, v in d.items()})
    return out


class PgPrivacy:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ------------------------------------------------------------ one person
    async def _gather(self, conn: asyncpg.Connection, p: Person) -> dict[str, Any]:
        conv_ids = [r["id"] for r in await conn.fetch(CONVERSATIONS, p.email, p.phone)]
        args = (p.email, p.phone, conv_ids)
        return {
            "conversation_ids": conv_ids,
            "conversations": await conn.fetch(
                "SELECT c.*, l.name, l.email, l.phone, l.company, l.job_title, l.need, l.company_size, "
                "l.current_systems, l.budget, l.timeline, l.status AS lead_status, l.crm_contact_id, l.crm_deal_id "
                "FROM conversations c LEFT JOIN leads l ON l.conversation_id = c.id "
                "WHERE c.id = ANY($1::uuid[]) ORDER BY c.started_at", conv_ids),
            "bookings": await conn.fetch(BOOKINGS, *args),
            "calls": await conn.fetch(CALLS, p.phone, conv_ids),
            "tickets": await conn.fetch(TICKETS, p.email, conv_ids),
            "call_requests": await conn.fetch(
                "SELECT * FROM call_requests WHERE conversation_id = ANY($1::uuid[]) ORDER BY created_at", conv_ids),
            "contacts": await conn.fetch(CONTACTS, p.email) if p.email else [],
        }

    @staticmethod
    def _outside(found: dict[str, Any]) -> list[str]:
        """Copies in other systems, for staff to remove there."""
        out = []
        for c in found["conversations"]:
            if c["crm_contact_id"]:
                out.append(f"HubSpot contact {c['crm_contact_id']}"
                           + (f" and deal {c['crm_deal_id']}" if c["crm_deal_id"] else ""))
        for b in found["bookings"]:
            if b["status"] == "confirmed":      # a cancelled booking's event was removed when it was cancelled
                out.append(f"Google Calendar event of booking {b['reference']} ({b['start_at']:%d %b %Y})")
        for t in found["tickets"]:
            if t["external_key"]:
                out.append(f"Jira issue {t['external_key']} (ticket {t['reference']})")
        if found["conversation_ids"] or found["bookings"]:
            out.append("Slack messages about this person (new lead, booking and handover posts)")
        return sorted(set(out), key=out.index)

    async def _blockers(self, conn: asyncpg.Connection, found: dict[str, Any]) -> list[str]:
        now = datetime.now(UTC)
        out = [f"Booking {b['reference']} on {b['start_at']:%d %b %Y %H:%M} UTC is still confirmed. "
               "Cancel it first (the person gets the cancellation email)."
               for b in found["bookings"] if b["status"] == "confirmed" and b["end_at"] > now]
        # Calendar and email work for a booking must finish (a cancelled call must leave the calendar). Other
        # waiting tasks (summaries, CRM sync, Slack posts) are simply deleted with the person's data.
        busy = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE (ref = ANY($1::text[]) AND status IN ('pending', 'running')) "
            "OR (ref = ANY($2::text[]) AND status = 'running')",
            [str(b["id"]) for b in found["bookings"]], [str(i) for i in found["conversation_ids"]])
        if busy:
            out.append(f"{busy} background task(s) for this person's bookings have not finished yet (calendar "
                       "or email updates). Try again in a few minutes.")
        return out

    async def find(self, p: Person) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            found = await self._gather(conn, p)
            blockers = await self._blockers(conn, found)
        now = datetime.now(UTC)
        return {
            "conversations": _plain(found["conversations"], ("id", "channel", "started_at", "last_message_at",
                                                              "name", "email", "phone", "company", "lead_status")),
            "bookings": [{**d, "upcoming": b["status"] == "confirmed" and b["end_at"] > now}
                         for b, d in zip(found["bookings"], _plain(found["bookings"], (
                             "id", "reference", "start_at", "status", "name", "email")), strict=True)],
            "calls": _plain(found["calls"], ("id", "direction", "created_at", "duration_s", "outcome")),
            "tickets": _plain(found["tickets"], ("reference", "client_name", "summary", "status", "created_at")),
            "call_requests": len(found["call_requests"]),
            "client_contact": _plain(found["contacts"], ("name", "role", "client_name")),
            "outside": self._outside(found),
            "blockers": blockers,
        }

    async def export(self, p: Person) -> dict[str, Any]:
        """Everything held about the person, readable (a copy for a subject access request)."""
        async with self.pool.acquire() as conn:
            found = await self._gather(conn, p)
            messages = await conn.fetch(
                "SELECT conversation_id, role, text, created_at FROM messages WHERE conversation_id = ANY($1::uuid[]) "
                "AND role IN ('user', 'assistant') AND text <> '' ORDER BY conversation_id, id",
                found["conversation_ids"])
        by_conv: dict[str, list[dict[str, Any]]] = {}
        for m in _plain(messages, ("conversation_id", "role", "text", "created_at")):
            by_conv.setdefault(m.pop("conversation_id"), []).append(
                {"from": "you" if m["role"] == "user" else "assistant", "text": m["text"], "at": m["created_at"]})
        conversations = []
        for c in _plain(found["conversations"], (
                "id", "channel", "started_at", "last_message_at", "page_url", "visitor_timezone", "verified_email",
                "name", "email", "phone", "company", "job_title", "need", "company_size", "current_systems",
                "budget", "timeline", "summary")):
            conversations.append({**{k: v for k, v in c.items() if v not in ("", None)},
                                  "messages": by_conv.get(c["id"], [])})
        return {
            "exported_at": datetime.now(UTC).isoformat(),
            "conversations": conversations,
            "call_requests": _plain(found["call_requests"], ("preferred_times", "timezone", "notes", "created_at")),
            "bookings": _plain(found["bookings"], ("reference", "start_at", "end_at", "status", "name", "email",
                                                   "phone", "visitor_timezone", "notes", "meeting_url",
                                                   "created_at")),
            "calls": _plain(found["calls"], ("direction", "from_number", "to_number", "created_at", "duration_s",
                                             "outcome", "purpose")),
            "tickets": _plain(found["tickets"], ("reference", "client_name", "summary", "details", "status",
                                                 "created_at")),
            "client_contact": _plain(found["contacts"], ("name", "role", "client_name")),
        }

    async def erase(self, p: Person, user_id: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(8402118)")      # one erasure at a time
            found = await self._gather(conn, p)
            blockers = await self._blockers(conn, found)
            if blockers:
                raise PrivacyError(" ".join(blockers))
            conv_ids = found["conversation_ids"]
            booking_ids = [b["id"] for b in found["bookings"]]
            call_ids = [c["id"] for c in found["calls"]]
            refs = [str(i) for i in conv_ids + booking_ids + call_ids]
            outside = self._outside(found)
            jobs = await conn.execute(
                "DELETE FROM jobs WHERE status <> 'running' AND (ref = ANY($1::text[]) "
                "OR ($2::text <> '' AND position($2::text in lower(payload::text)) > 0))", refs, p.email)
            logs = await conn.execute(
                "DELETE FROM events WHERE kind NOT LIKE 'privacy_%' AND (conversation_id = ANY($1::uuid[]) "
                "OR ($2::text <> '' AND position($2::text in lower(data::text)) > 0))", conv_ids, p.email)
            bookings = await conn.execute("DELETE FROM bookings WHERE id = ANY($1::uuid[])", booking_ids)
            calls = await conn.execute("DELETE FROM calls WHERE id = ANY($1::uuid[])", call_ids)
            tickets = await conn.execute(
                f"UPDATE tickets SET contact_email = '', updated_at = now() WHERE {_email('contact_email')}",
                p.email)
            conversations = await conn.execute("DELETE FROM conversations WHERE id = ANY($1::uuid[])", conv_ids)
            counts = {"conversations": _n(conversations), "bookings": _n(bookings), "calls": _n(calls),
                      "tickets_anonymised": _n(tickets), "background_tasks": _n(jobs), "log_entries": _n(logs)}
            kept = [f"Client contact at {c['client_name']} (kept: it is how they sign in as a client; remove it "
                    "from the client's contacts if they have left)" for c in found["contacts"]]
            if found["tickets"]:
                kept.append("Support tickets (kept as the client company's record; the contact email is removed)")
            await conn.execute(
                "INSERT INTO events (conversation_id, kind, data) VALUES (NULL, 'privacy_erased', $1::jsonb)",
                json.dumps({"user": user_id, "person": p.fingerprint, "counts": counts, "outside": outside}))
        return {"counts": counts, "outside": outside, "kept": kept}

    # ------------------------------------------------------------ retention
    async def purge(self, r: Retention, now: datetime | None = None, user_id: str = "") -> dict[str, int]:
        now = now or datetime.now(UTC)
        out: dict[str, int] = {}

        def before(days: int) -> datetime:
            return now - timedelta(days=days)

        async with self.pool.acquire() as conn, conn.transaction():
            if r.conversations_days:
                cut = before(r.conversations_days)
                out["conversations"] = _n(await conn.execute(
                    "DELETE FROM conversations WHERE last_message_at < $1", cut))
                out["calls"] = _n(await conn.execute(
                    "DELETE FROM calls WHERE coalesce(ended_at, created_at) < $1", cut))
            if r.bookings_days:
                out["bookings"] = _n(await conn.execute(
                    "DELETE FROM bookings WHERE end_at < $1", before(r.bookings_days)))
            if r.logs_days:
                out["log_entries"] = _n(await conn.execute(
                    "DELETE FROM events WHERE created_at < $1 AND kind NOT LIKE 'privacy_%'", before(r.logs_days)))
            if r.finished_jobs_days:
                out["background_tasks"] = _n(await conn.execute(
                    "DELETE FROM jobs WHERE status IN ('done', 'failed', 'cancelled') "
                    "AND coalesce(finished_at, created_at) < $1", before(r.finished_jobs_days)))
            out["expired_sign_ins"] = _n(await conn.execute("DELETE FROM staff_sessions WHERE expires_at < $1", now))
            await conn.execute(
                "INSERT INTO events (conversation_id, kind, data) VALUES (NULL, 'privacy_retention', $1::jsonb)",
                json.dumps({"deleted": out, "settings": r.describe(), **({"user": user_id} if user_id else {})}))
        return out

    async def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT e.kind, e.data, e.created_at, s.name AS by_name FROM events e "
            "LEFT JOIN staff_users s ON s.id::text = e.data->>'user' "
            "WHERE e.kind LIKE 'privacy_%' ORDER BY e.id DESC LIMIT $1", limit)
        return [{"kind": r["kind"], "data": json.loads(r["data"]) if isinstance(r["data"], str) else r["data"],
                 "at": r["created_at"].isoformat(), "by": r["by_name"] or ""} for r in rows]


def _n(status: str) -> int:
    """'DELETE 3' / 'UPDATE 1' -> 3 / 1."""
    return int(status.rsplit(" ", 1)[-1]) if status and status[-1].isdigit() else 0
