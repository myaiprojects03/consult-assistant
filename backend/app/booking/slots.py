"""Which discovery-call times can be offered: pure date arithmetic, no I/O, fully unit-tested.

Slots follow the office calendar (days, opening hours, time zone with daylight saving) and must:
start after a minimum notice, fall within the booking horizon, and keep a buffer around anything
already in the consultant's calendar. Times are shown to the visitor in their own time zone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Interval = tuple[datetime, datetime]
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class BookingConfig:
    timezone: str = "Europe/London"
    days: tuple[int, ...] = (0, 1, 2, 3, 4)
    start: time = time(9, 0)
    end: time = time(17, 30)            # the last call must end by this time
    slot_minutes: int = 30
    buffer_minutes: int = 15
    min_notice_hours: int = 12
    days_ahead: int = 14
    max_offered: int = 6
    reminders: tuple[tuple[str, int], ...] = field(default=(("reminder_24h", 24 * 60), ("reminder_1h", 60)))

    @classmethod
    def from_rules(cls, data: dict[str, Any] | None) -> BookingConfig:
        d = dict(data or {})
        if "days" in d:
            d["days"] = tuple(DAY_NAMES.index(x.strip().lower()[:3]) for x in d["days"])
        for k in ("start", "end"):
            if k in d:
                d[k] = time.fromisoformat(str(d[k]))
        if "reminders" in d:
            d["reminders"] = tuple((str(k), int(v)) for k, v in d["reminders"].items())
        cfg = cls(**d)
        cfg.tz  # noqa: B018 - fail at startup on an unknown time zone
        return cfg

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def slot(self) -> timedelta:
        return timedelta(minutes=self.slot_minutes)


def zone(name: str | None, fallback: str) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else ZoneInfo(fallback)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(fallback)


def overlaps(start: datetime, end: datetime, busy: list[Interval]) -> bool:
    return any(s < end and start < e for s, e in busy)


def free_slots(cfg: BookingConfig, *, now: datetime, busy: list[Interval], window_start: datetime,
               window_end: datetime) -> list[datetime]:
    """Bookable call starts (aware datetimes in office time) between window_start and window_end."""
    tz, slot, buffer = cfg.tz, cfg.slot, timedelta(minutes=cfg.buffer_minutes)
    earliest = max(now + timedelta(hours=cfg.min_notice_hours), window_start)
    latest = min(now + timedelta(days=cfg.days_ahead), window_end)
    out: list[datetime] = []
    day = earliest.astimezone(tz).date()
    while day <= latest.astimezone(tz).date():
        if day.weekday() in cfg.days:
            t = datetime.combine(day, cfg.start, tz)
            close = datetime.combine(day, cfg.end, tz)
            while t + slot <= close:
                if t >= earliest and t + slot <= latest and not overlaps(t - buffer, t + slot + buffer, busy):
                    out.append(t)
                t += slot
        day += timedelta(days=1)
    return out


def day_window(day_from: date, day_to: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Whole days in the visitor's time zone, as an instant range."""
    return datetime.combine(day_from, time(0), tz), datetime.combine(day_to + timedelta(days=1), time(0), tz)


def choose(slots: list[datetime], *, visitor_tz: ZoneInfo, preferred: time | None, limit: int) -> list[datetime]:
    """A short, useful choice: the closest times to a stated preference, otherwise a spread over the
    first available days (at most two per day) rather than six times on the same morning."""
    if preferred is not None:
        def distance(s: datetime) -> tuple[date, int]:
            local = s.astimezone(visitor_tz)
            minutes = local.hour * 60 + local.minute
            return local.date(), abs(minutes - (preferred.hour * 60 + preferred.minute))
        by_day: dict[date, list[datetime]] = {}
        for s in slots:
            by_day.setdefault(s.astimezone(visitor_tz).date(), []).append(s)
        ordered = [s for d in sorted(by_day) for s in sorted(by_day[d], key=lambda x: distance(x)[1])[:2]]
        return sorted(ordered[:limit])
    picked: list[datetime] = []
    per_day: dict[date, int] = {}
    for s in slots:
        d = s.astimezone(visitor_tz).date()
        if per_day.get(d, 0) < 2:
            picked.append(s)
            per_day[d] = per_day.get(d, 0) + 1
        if len(picked) == limit:
            break
    return picked


def zone_label(tz: ZoneInfo) -> str:
    key = tz.key
    return "UTC" if key in ("UTC", "Etc/UTC") else key.split("/")[-1].replace("_", " ") + " time"


def describe(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """'Tuesday 18 November, 14:00-14:30 (London time)'"""
    s, e = start.astimezone(tz), end.astimezone(tz)
    return f"{s:%A} {s.day} {s:%B}, {s:%H:%M}-{e:%H:%M} ({zone_label(tz)})"
