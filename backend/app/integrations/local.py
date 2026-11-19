"""Stand-ins used when an integration is switched off: every action is written to the event log, so the
full flow (booking, emails, CRM records, team alerts) can be run and checked without any accounts."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.db.repo import Repo
from app.integrations.base import (
    CRM,
    Calendar,
    CalendarEvent,
    CRMRecord,
    CRMResult,
    Interval,
    Mailer,
    Notifier,
    TextMessages,
    TicketInfo,
    Ticketing,
    TicketState,
)

log = logging.getLogger(__name__)


class _Logged:
    def __init__(self, repo: Repo):
        self.repo = repo

    async def _log(self, action: str, data: dict[str, Any]) -> None:
        log.info("[%s off] %s %s", self.name, action, data.get("summary", ""))   # type: ignore[attr-defined]
        await self.repo.log_event(None, "integration_off", {"integration": self.name,  # type: ignore[attr-defined]
                                                            "action": action, **data})


class LocalCRM(_Logged, CRM):
    name = "off (logged)"

    async def sync(self, record: CRMRecord) -> CRMResult:
        await self._log("sync", {"email": record.email, "status": record.lead_status, "booked": record.booked})
        return CRMResult(contact_id=record.contact_id or f"local-{record.email}",
                         deal_id=record.deal_id or ("local-deal" if record.booked else ""), note_id="local-note")


class LocalCalendar(_Logged, Calendar):
    """Bookings live in the database only; there are no outside events to avoid."""
    name = "local (database only)"

    async def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Interval]]:
        return {c: [] for c in calendar_ids}

    async def create_event(self, event: CalendarEvent) -> str:
        await self._log("create_event", {"event_id": event.event_id, "start": event.start.isoformat()})
        return ""

    async def move_event(self, event: CalendarEvent) -> str:
        await self._log("move_event", {"event_id": event.event_id, "start": event.start.isoformat()})
        return ""

    async def cancel_event(self, calendar_id: str, event_id: str) -> None:
        await self._log("cancel_event", {"event_id": event_id})


class LocalMailer(_Logged, Mailer):
    name = "off (logged)"

    async def send(self, *, to_email: str, to_name: str, subject: str, text: str) -> None:
        await self._log("send", {"to": to_email, "subject": subject, "summary": subject})


class LocalNotifier(_Logged, Notifier):
    name = "off (logged)"

    async def post(self, channel: str, text: str) -> None:
        await self._log("post", {"channel": channel, "text": text[:1000], "summary": text[:80]})


class LocalSMS(_Logged, TextMessages):
    name = "off (logged)"

    async def send(self, to: str, text: str) -> None:
        await self._log("sms", {"to": to[:-4] + "****" if len(to) > 4 else to, "text": text[:320],
                                "summary": text[:80]})


class LocalTicketing(_Logged, Ticketing):
    """Tickets live in the database only; their status is what staff set there."""
    name = "off (database only)"
    external = False

    async def create(self, t: TicketInfo) -> tuple[str, str]:
        await self._log("create_ticket", {"reference": t.reference, "priority": t.priority, "summary": t.summary})
        return "", ""

    async def comment(self, key: str, text: str, marker: str) -> None:
        await self._log("comment", {"key": key, "marker": marker})

    async def state(self, key: str) -> TicketState | None:
        return None
