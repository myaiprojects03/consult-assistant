"""Test doubles: a scripted model and a deterministic embedder."""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.integrations.base import (
    CRM,
    Calendar,
    CalendarEvent,
    CRMRecord,
    CRMResult,
    IntegrationError,
    Mailer,
    Notifier,
    Telephony,
    TextMessages,
    TicketInfo,
    Ticketing,
    TicketState,
)
from app.kb.embeddings import Embedder
from app.llm.base import Done, Event, LLMError, LLMProvider, Message, TextDelta, ToolCall, ToolCallEvent, ToolSpec


class ScriptedLLM(LLMProvider):
    """Plays back one scripted round per call. A round is a list of str (text pieces) and ToolCall items.
    Records what it was sent, so tests can check the prompt, tools and history."""
    name, model = "Scripted", "scripted-1"

    def __init__(self, rounds: list[list[Any]], fail: str | None = None):
        self.rounds = list(rounds)
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
                     context: str = "") -> AsyncIterator[Event]:
        self.calls.append({"system": system, "context": context, "messages": list(messages),
                           "tools": [t.name for t in tools]})
        if self.fail:
            raise LLMError(self.fail)
        script = self.rounds.pop(0) if self.rounds else ["(no more script)"]
        has_tool = False
        for item in script:
            if isinstance(item, ToolCall):
                has_tool = True
                yield ToolCallEvent(item)
            else:
                yield TextDelta(item)
        yield Done("tool_use" if has_tool else "end", 10, 5)


class HashEmbedder(Embedder):
    """Bag-of-words hashed into 384 dims: similar texts get similar vectors, with no model download."""

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0  # noqa: S324
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


# ---------------------------------------------------------------- integrations that record what they were asked
class FakeCRM(CRM):
    name = "fake CRM"

    def __init__(self) -> None:
        self.records: list[CRMRecord] = []
        self.fail_with: Exception | None = None

    async def sync(self, record: CRMRecord) -> CRMResult:
        if self.fail_with:
            raise self.fail_with
        self.records.append(record)
        return CRMResult(contact_id=record.contact_id or "c-1",
                         deal_id=record.deal_id or ("d-1" if record.booked else ""),
                         note_id=record.note_id or "n-1")


class FakeCalendar(Calendar):
    name = "fake calendar"

    def __init__(self) -> None:
        self.busy_times: list[tuple[datetime, datetime]] = []
        self.created: list[CalendarEvent] = []
        self.moved: list[CalendarEvent] = []
        self.cancelled: list[str] = []
        self.down = False

    async def busy(self, calendar_ids, start, end):
        if self.down:
            raise IntegrationError("calendar down")
        return {c: list(self.busy_times) for c in calendar_ids}

    async def create_event(self, event: CalendarEvent) -> str:
        self.created.append(event)
        return f"https://meet.google.com/{event.event_id[:10]}"

    async def move_event(self, event: CalendarEvent) -> str:
        self.moved.append(event)
        return f"https://meet.google.com/{event.event_id[:10]}"

    async def cancel_event(self, calendar_id: str, event_id: str) -> None:
        self.cancelled.append(event_id)


class FakeMailer(Mailer):
    name = "fake email"

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to_email: str, to_name: str, subject: str, text: str) -> None:
        self.sent.append({"to": to_email, "subject": subject, "text": text})


class FakeNotifier(Notifier):
    name = "fake Slack"

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def post(self, channel: str, text: str) -> None:
        self.posts.append((channel, text))


class FakeTicketing(Ticketing):
    """Behaves like Jira: issue keys SUP-1, SUP-2..., status set by the test."""
    name = "fake Jira"
    external = True

    def __init__(self) -> None:
        self.created: list[TicketInfo] = []
        self.comments: list[tuple[str, str, str]] = []
        self.states: dict[str, TicketState] = {}

    async def create(self, t: TicketInfo) -> tuple[str, str]:
        self.created.append(t)
        key = f"SUP-{len(self.created)}"
        return key, f"https://example.atlassian.net/browse/{key}"

    async def comment(self, key: str, text: str, marker: str) -> None:
        if not any(m == marker for _, _, m in self.comments):
            self.comments.append((key, text, marker))

    async def state(self, key: str) -> TicketState | None:
        return self.states.get(key)


class FakeSMS(TextMessages):
    name = "fake SMS"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))


class FakeTelephony(Telephony):
    name = "fake Twilio"

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail_with: Exception | None = None

    async def create_call(self, *, to: str, twiml_doc: str, status_url: str) -> str:
        if self.fail_with:
            raise self.fail_with
        self.calls.append({"to": to, "twiml": twiml_doc, "status_url": status_url})
        return f"CA{len(self.calls):032d}"


@dataclass
class FakeIntegrations:
    crm: FakeCRM
    calendar: FakeCalendar
    mailer: FakeMailer
    notifier: FakeNotifier
    ticketing: FakeTicketing
    sms: FakeSMS
    telephony: FakeTelephony

    @classmethod
    def install(cls, state) -> FakeIntegrations:
        f = cls(FakeCRM(), FakeCalendar(), FakeMailer(), FakeNotifier(), FakeTicketing(), FakeSMS(),
                FakeTelephony())
        integ = state.integrations
        integ.crm, integ.calendar, integ.mailer, integ.notifier = f.crm, f.calendar, f.mailer, f.notifier
        integ.ticketing = f.ticketing
        integ.sms = f.sms
        if integ.telephony is not None:      # phone calls on (VOICE_PROVIDER=twilio): record instead of dialling
            integ.telephony = f.telephony
        state.booking.calendar = f.calendar
        state.verifier.mailer = f.mailer
        state.tickets.ticketing = f.ticketing
        return f
