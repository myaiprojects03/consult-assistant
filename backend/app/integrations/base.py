"""What the assistant needs from the outside world, independent of the vendor.

Each integration has an interface here, a "local" implementation that only writes to the event log
(so everything works without accounts), and one real implementation per vendor. Which one runs is a
setting (CRM_PROVIDER, CALENDAR_PROVIDER, EMAIL_PROVIDER, SLACK_BOT_TOKEN); adding Zoho, Outlook or Teams
means one new class, not changes to the assistant.

Integrations are only called from background jobs (app/jobs), never while the visitor waits, except the
calendar's free/busy check, which booking needs to answer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

Interval = tuple[datetime, datetime]


class IntegrationError(RuntimeError):
    """A failed call to an outside service. `retryable` is False for problems a retry cannot fix
    (wrong key, missing permission, bad input): those fail at once and raise an alert."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------- interfaces
@dataclass
class CRMRecord:
    """Everything the CRM gets about one conversation. Empty values never overwrite CRM data."""
    email: str
    name: str = ""
    phone: str = ""
    company: str = ""
    job_title: str = ""
    lead_status: str = "new"                 # new | qualified | not_a_fit
    booked: bool = False
    deal_name: str = ""
    note_html: str = ""                      # lead details, summary and booking, as simple HTML
    contact_id: str = ""                     # ids from an earlier sync: update instead of create
    deal_id: str = ""
    note_id: str = ""


@dataclass
class CRMResult:
    contact_id: str
    deal_id: str = ""
    note_id: str = ""
    contact_url: str = ""


class CRM(ABC):
    name = "crm"

    @abstractmethod
    async def sync(self, record: CRMRecord) -> CRMResult: ...


@dataclass
class CalendarEvent:
    calendar_id: str
    event_id: str                            # fixed per booking: a retried create cannot duplicate
    title: str
    description: str
    start: datetime
    end: datetime
    timezone: str
    attendees: list[tuple[str, str]] = field(default_factory=list)   # (email, display name)


class Calendar(ABC):
    name = "calendar"

    @abstractmethod
    async def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Interval]]:
        """Busy times per calendar between start and end (outside events, not our own bookings)."""
    @abstractmethod
    async def create_event(self, event: CalendarEvent) -> str:
        """Create the event with a video meeting; returns the meeting link ("" if none)."""
    @abstractmethod
    async def move_event(self, event: CalendarEvent) -> str:
        """Move or update the event; returns the meeting link ("" if none)."""
    @abstractmethod
    async def cancel_event(self, calendar_id: str, event_id: str) -> None: ...


class Mailer(ABC):
    name = "email"

    @abstractmethod
    async def send(self, *, to_email: str, to_name: str, subject: str, text: str) -> None: ...


@dataclass
class TicketInfo:
    """Everything the ticketing system needs for a new ticket."""
    reference: str              # ours, e.g. TK-7K3M9Q; also used to find a ticket a retry already made
    client_name: str
    client_slug: str
    contact_email: str
    summary: str
    details: str
    priority: str
    category: str


@dataclass
class TicketState:
    status: str                 # the system's own wording, e.g. "In Progress"
    stage: str                  # ours: open | in_progress | resolved
    assignee: str = ""
    updated: str = ""


class Ticketing(ABC):
    name = "ticketing"
    external = True             # False for the stand-in: tickets then live only in our database

    @abstractmethod
    async def create(self, t: TicketInfo) -> tuple[str, str]:
        """Create the ticket (or find the one an earlier attempt made); returns (key, url)."""
    @abstractmethod
    async def comment(self, key: str, text: str, marker: str) -> None:
        """Add a comment once: a comment carrying `marker` is not added twice."""
    @abstractmethod
    async def state(self, key: str) -> TicketState | None: ...


class Notifier(ABC):
    """Team messaging (Slack now; Teams would be another implementation)."""
    name = "notifier"

    @abstractmethod
    async def post(self, channel: str, text: str) -> None: ...


class TextMessages(ABC):
    """SMS to a phone number (Twilio now)."""
    name = "sms"

    @abstractmethod
    async def send(self, to: str, text: str) -> None: ...


class Telephony(ABC):
    """Placing phone calls (Twilio now). Answering them is the voice webhook's job (app/api/voice.py)."""
    name = "telephony"

    @abstractmethod
    async def create_call(self, *, to: str, twiml_doc: str, status_url: str) -> str:
        """Start a call that runs `twiml_doc` when answered; returns the provider's call id."""


@dataclass
class Integrations:
    crm: CRM
    calendar: Calendar
    mailer: Mailer
    notifier: Notifier
    ticketing: Ticketing
    sms: TextMessages
    channels: dict[str, str]                 # leads | handovers | support | alerts -> channel
    _clients: list[httpx.AsyncClient] = field(default_factory=list)
    telephony: Telephony | None = None        # None: no phone calls (browser voice still works)

    def describe(self) -> dict[str, str]:
        return {"crm": self.crm.name, "calendar": self.calendar.name, "email": self.mailer.name,
                "team_messages": self.notifier.name, "tickets": self.ticketing.name, "sms": self.sms.name,
                "phone_calls": self.telephony.name if self.telephony else "off (browser voice only)"}

    async def alert(self, text: str) -> None:
        """Best effort: an integration failure must never break what reported it."""
        try:
            await self.notifier.post(self.channels["alerts"], text)
        except Exception:  # noqa: S110 - already failing; the event log has the details
            pass

    async def aclose(self) -> None:
        for c in self._clients:
            await c.aclose()


# ---------------------------------------------------------------- HTTP helper
async def call(client: httpx.AsyncClient, method: str, url: str, *, service: str,
               ok: tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
    """One HTTP call to a vendor API with errors sorted into retryable (network, rate limit, server error)
    and permanent (anything else). Returns the response for any status listed in `ok`."""
    try:
        r = await client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise IntegrationError(f"{service}: could not connect ({type(exc).__name__})") from exc
    if r.status_code in ok:
        return r
    detail = r.text[:300].replace("\n", " ")
    if r.status_code == 429 or r.status_code >= 500:
        raise IntegrationError(f"{service}: HTTP {r.status_code}: {detail}")
    if r.status_code in (401, 403):
        raise IntegrationError(f"{service}: access refused (HTTP {r.status_code}); check the key and its "
                               f"permissions: {detail}", retryable=False)
    raise IntegrationError(f"{service}: HTTP {r.status_code}: {detail}", retryable=False)
