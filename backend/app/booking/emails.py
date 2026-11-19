"""Emails about discovery calls. The wording lives in content/emails.yaml so staff can change it without
touching code; placeholders like {when} are filled in here. Unknown placeholders are left out, never an error."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.booking.slots import BookingConfig, describe, zone
from app.booking.store import Booking
from app.booking.team import Consultant

TEMPLATES = ("confirmation", "rescheduled", "cancelled", "reminder_24h", "reminder_1h")


@dataclass
class EmailTemplate:
    subject: str
    body: str


class _Blank(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


def load_templates(content_dir: Path) -> dict[str, EmailTemplate]:
    data = yaml.safe_load((content_dir / "emails.yaml").read_text(encoding="utf-8")) or {}
    missing = [t for t in TEMPLATES if t not in data]
    if missing:
        raise ValueError(f"content/emails.yaml is missing: {', '.join(missing)}")
    return {k: EmailTemplate(v["subject"].strip(), v["body"].strip() + "\n") for k, v in data.items()}


def render(t: EmailTemplate, *, booking: Booking, consultant: Consultant, cfg: BookingConfig,
           firm_name: str) -> tuple[str, str]:
    tz = zone(booking.visitor_timezone, cfg.timezone)
    values = _Blank(
        name=booking.name, first_name=booking.name.split()[0] if booking.name.strip() else "there",
        firm=firm_name, consultant=consultant.name, reference=booking.reference,
        when=describe(booking.start_at, booking.end_at, tz),
        when_office=describe(booking.start_at, booking.end_at, cfg.tz),
        minutes=cfg.slot_minutes,
        video=(f"Video link: {booking.meeting_url}" if booking.meeting_url
               else "Video link: in the calendar invitation"))
    return t.subject.format_map(values), t.body.format_map(values)


def load_sms(content_dir: Path) -> dict[str, str]:
    """Text message wording (content/sms.yaml); missing file or template = that message is not sent."""
    path = content_dir / "sms.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {k: " ".join(str(v).split()) for k, v in (data or {}).items() if v and str(v).strip()}


def render_sms(text: str, *, booking: Booking, consultant: Consultant, cfg: BookingConfig, firm_name: str) -> str:
    return render(EmailTemplate("", text), booking=booking, consultant=consultant, cfg=cfg, firm_name=firm_name)[1]
