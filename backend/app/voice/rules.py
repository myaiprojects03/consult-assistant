"""Voice rules from content/rules.yaml: greetings, call length, and the hours for live transfers and
outbound calls. Times are checked in code, never left to the model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.booking.slots import DAY_NAMES


@dataclass(frozen=True)
class Window:
    """Weekly opening hours, e.g. Monday to Friday 09:00-18:00."""
    days: tuple[int, ...] = (0, 1, 2, 3, 4)
    start: time = time(9, 0)
    end: time = time(18, 0)

    @classmethod
    def parse(cls, data: dict[str, Any] | None, default: Window) -> Window:
        if not data:
            return default
        days = tuple(DAY_NAMES.index(str(d).strip().lower()[:3]) for d in data.get("days", [])) or default.days
        return cls(days, time.fromisoformat(str(data.get("start", default.start.isoformat()))),
                   time.fromisoformat(str(data.get("end", default.end.isoformat()))))

    def is_open(self, now: datetime, tz: ZoneInfo) -> bool:
        local = now.astimezone(tz)
        return local.weekday() in self.days and self.start <= local.time().replace(tzinfo=None) < self.end

    def next_open(self, now: datetime, tz: ZoneInfo) -> datetime:
        """`now` if open, else the next opening time (as an aware datetime)."""
        if self.is_open(now, tz):
            return now
        local = now.astimezone(tz)
        for add in range(0, 8):
            day = (local + timedelta(days=add)).date()
            if day.weekday() not in self.days:
                continue
            opening = datetime.combine(day, self.start, tzinfo=tz)
            if opening > local:
                return opening
        raise ValueError("calling hours have no open day")


@dataclass(frozen=True)
class VoiceRules:
    greeting: str = "Hello, how can I help you today?"
    outbound_greeting: str = "Hello {name}, I'm calling about {purpose}. Is now a good time to talk?"
    language: str = "en-GB"
    max_call_minutes: int = 15
    transfer_hours: Window = field(default_factory=Window)
    calling_hours: Window = field(default_factory=lambda: Window(start=time(10, 0), end=time(17, 0)))

    @classmethod
    def from_rules(cls, data: dict[str, Any] | None) -> VoiceRules:
        d = dict(data or {})
        base = cls()
        return cls(
            greeting=" ".join(str(d.get("greeting", base.greeting)).split()),
            outbound_greeting=" ".join(str(d.get("outbound_greeting", base.outbound_greeting)).split()),
            language=str(d.get("language", base.language)),
            max_call_minutes=int(d.get("max_call_minutes", base.max_call_minutes)),
            transfer_hours=Window.parse(d.get("transfer_hours"), base.transfer_hours),
            calling_hours=Window.parse(d.get("calling_hours"), base.calling_hours))

    def outbound_text(self, name: str, purpose: str) -> str:
        text = self.outbound_greeting.replace("{name}", name or "there").replace("{purpose}", purpose or "your enquiry")
        return " ".join(text.replace(" ,", ",").split())
