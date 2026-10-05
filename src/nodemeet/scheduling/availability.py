"""Weekly availability rules per host."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore

Window = Tuple[time, time]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _parse_time(h: str, m: str) -> time:
    hour, minute = int(h), int(m)
    if hour == 24 and minute == 0:
        return time(23, 59, 59, 999999)  # "until midnight"
    return time(hour, minute)


def parse_windows(value: Union[str, Sequence[Any]]) -> List[Window]:
    """Parse ``"09:00-12:00, 13:00-17:00"`` or ``[("09:00","12:00"), ...]``."""
    items: Iterable[Any]
    if isinstance(value, str):
        items = [p for p in value.split(",") if p.strip()]
    else:
        items = value
    out: List[Window] = []
    for item in items:
        if isinstance(item, str):
            m = _RANGE_RE.match(item)
            if not m:
                raise ValueError(f"bad time range {item!r}; expected HH:MM-HH:MM")
            start, end = _parse_time(m[1], m[2]), _parse_time(m[3], m[4])
        else:
            a, b = item
            start = a if isinstance(a, time) else _parse_time(*str(a).split(":"))
            end = b if isinstance(b, time) else _parse_time(*str(b).split(":"))
        if end <= start:
            raise ValueError(f"window end must be after start: {item!r}")
        out.append((start, end))
    return sorted(out)


def parse_days(key: Union[str, int]) -> List[int]:
    """``"mon-fri"`` -> [0..4], ``"sat,sun"`` -> [5, 6], ``2`` -> [2]."""
    if isinstance(key, int):
        return [key]
    days: List[int] = []
    for part in str(key).lower().split(","):
        part = part.strip()[:7]
        if "-" in part:
            a, b = (p.strip()[:3] for p in part.split("-", 1))
            i, j = WEEKDAYS.index(a), WEEKDAYS.index(b)
            days.extend(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        elif part:
            days.append(WEEKDAYS.index(part[:3]))
    return days


def _fmt_windows(windows: List[Window]) -> List[List[str]]:
    return [[s.strftime("%H:%M"), "24:00" if e >= time(23, 59, 59) else e.strftime("%H:%M")]
            for s, e in windows]


@dataclass
class Availability:
    """When a host can be booked.

    All times in ``weekly``/``overrides`` are wall-clock times in ``timezone``,
    so DST is handled automatically.

    >>> av = Availability.from_hours("host_1", "Asia/Kolkata",
    ...                              {"mon-fri": "09:00-12:00, 14:00-18:00"},
    ...                              duration_minutes=30, buffer_after=10)
    """

    host_id: str
    timezone: str = "UTC"
    weekly: Dict[int, List[Window]] = field(default_factory=dict)  # 0 = Monday
    duration_minutes: int = 30
    step_minutes: Optional[int] = None  # distance between slot starts; defaults to duration
    buffer_before: int = 0  # minutes kept free before each meeting
    buffer_after: int = 0  # minutes kept free after each meeting
    min_notice_minutes: int = 0  # how soon the earliest slot can be
    max_days_ahead: int = 60  # booking horizon
    blackout_dates: Set[date] = field(default_factory=set)
    overrides: Dict[date, List[Window]] = field(default_factory=dict)  # replace hours on a date
    title: str = "Meeting"
    host_name: str = ""
    host_email: str = ""
    tenant_id: Optional[str] = None
    price: int = 0  # in the currency's smallest unit (cents, paise); 0 = free
    currency: str = "USD"
    payment_hold_minutes: int = 30  # unpaid bookings are released after this (Stripe minimum)
    collect_phone: bool = False  # ask for a phone number (SMS / WhatsApp reminders)

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {self.timezone!r}") from None
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        self.blackout_dates = {d if isinstance(d, date) else date.fromisoformat(str(d))
                               for d in self.blackout_dates}

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def step(self) -> int:
        return self.step_minutes or self.duration_minutes

    @classmethod
    def from_hours(cls, host_id: str, timezone: str = "UTC",
                   hours: Optional[Mapping[Union[str, int], Any]] = None, **kwargs: Any
                   ) -> "Availability":
        weekly: Dict[int, List[Window]] = {}
        for key, value in (hours or {}).items():
            for day in parse_days(key):
                weekly.setdefault(day, []).extend(parse_windows(value))
        weekly = {d: sorted(w) for d, w in weekly.items()}
        overrides = {(d if isinstance(d, date) else date.fromisoformat(str(d))): parse_windows(v)
                     for d, v in (kwargs.pop("overrides", None) or {}).items()}
        return cls(host_id=host_id, timezone=timezone, weekly=weekly, overrides=overrides, **kwargs)

    def windows_for(self, day: date) -> List[Window]:
        if day in self.blackout_dates:
            return []
        if day in self.overrides:
            return self.overrides[day]
        return self.weekly.get(day.weekday(), [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host_id": self.host_id, "timezone": self.timezone,
            "weekly": {WEEKDAYS[d]: _fmt_windows(w) for d, w in sorted(self.weekly.items())},
            "duration_minutes": self.duration_minutes, "step_minutes": self.step_minutes,
            "buffer_before": self.buffer_before, "buffer_after": self.buffer_after,
            "min_notice_minutes": self.min_notice_minutes, "max_days_ahead": self.max_days_ahead,
            "blackout_dates": sorted(d.isoformat() for d in self.blackout_dates),
            "overrides": {d.isoformat(): _fmt_windows(w) for d, w in sorted(self.overrides.items())},
            "title": self.title, "host_name": self.host_name, "host_email": self.host_email,
            "tenant_id": self.tenant_id, "price": self.price, "currency": self.currency,
            "payment_hold_minutes": self.payment_hold_minutes, "collect_phone": self.collect_phone,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Availability":
        data = dict(d)
        weekly = data.pop("weekly", {}) or {}
        extra = {k: data[k] for k in (
            "duration_minutes", "step_minutes", "buffer_before", "buffer_after",
            "min_notice_minutes", "max_days_ahead", "blackout_dates", "overrides",
            "title", "host_name", "host_email", "tenant_id", "price", "currency",
            "payment_hold_minutes", "collect_phone") if k in data and data[k] is not None}
        return cls.from_hours(data["host_id"], data.get("timezone", "UTC"), weekly, **extra)
