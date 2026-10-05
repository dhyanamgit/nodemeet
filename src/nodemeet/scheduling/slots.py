"""Slot finding: turn availability + busy times into bookable slots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ..models import fmt_dt, to_utc
from .availability import Availability, ZoneInfo

Interval = Tuple[datetime, datetime]
_END_OF_DAY = time(23, 59, 59)


@dataclass(frozen=True)
class Slot:
    start: datetime  # aware, UTC
    end: datetime  # aware, UTC

    def local(self, tz: str) -> Tuple[datetime, datetime]:
        z = ZoneInfo(tz)
        return self.start.astimezone(z), self.end.astimezone(z)

    def to_dict(self, tz: Optional[str] = None) -> Dict[str, Any]:
        d: Dict[str, Any] = {"start": fmt_dt(self.start), "end": fmt_dt(self.end)}
        if tz:
            s, e = self.local(tz)
            d.update(local_start=s.isoformat(), local_end=e.isoformat(), timezone=tz)
        return d


def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def _as_local_date(value: Union[date, datetime], tz: ZoneInfo) -> date:
    if isinstance(value, datetime):
        return to_utc(value).astimezone(tz).date()
    return value


def is_free(start: datetime, end: datetime, busy: Iterable[Interval], *,
            buffer_before: int = 0, buffer_after: int = 0) -> bool:
    """True if ``[start, end)`` plus buffers does not collide with any busy interval.

    Buffers are applied symmetrically: the new meeting needs its own buffers
    free, and must not sit inside the buffers of an existing meeting.
    """
    bb, ba = timedelta(minutes=buffer_before), timedelta(minutes=buffer_after)
    for b0, b1 in busy:
        b0, b1 = to_utc(b0), to_utc(b1)
        if _overlaps(start - bb, end + ba, b0, b1) or _overlaps(start, end, b0 - bb, b1 + ba):
            return False
    return True


def find_slots(availability: Availability, start: Union[date, datetime],
               end: Union[date, datetime], *, busy: Sequence[Interval] = (),
               duration_minutes: Optional[int] = None, now: Optional[datetime] = None,
               limit: Optional[int] = None) -> List[Slot]:
    """Return free slots between ``start`` and ``end`` (inclusive dates in host time zone).

    ``busy`` is any iterable of ``(start, end)`` aware datetimes: existing
    bookings, events from your own calendar system, etc.
    """
    av = availability
    tz = av.tz
    now = to_utc(now) if now else datetime.now(timezone.utc)
    duration = timedelta(minutes=duration_minutes or av.duration_minutes)
    step = timedelta(minutes=av.step if duration_minutes is None else
                     (av.step_minutes or duration_minutes))
    earliest = now + timedelta(minutes=av.min_notice_minutes)
    latest = now + timedelta(days=av.max_days_ahead)
    busy_list = sorted((to_utc(a), to_utc(b)) for a, b in busy)
    first, last = _as_local_date(start, tz), _as_local_date(end, tz)
    range_start = to_utc(start) if isinstance(start, datetime) else None
    range_end = to_utc(end) if isinstance(end, datetime) else None

    slots: List[Slot] = []
    day = first
    while day <= last:
        for w_start, w_end in av.windows_for(day):
            win_s = datetime.combine(day, w_start).replace(tzinfo=tz)
            if w_end >= _END_OF_DAY:
                win_e = datetime.combine(day + timedelta(days=1), time(0)).replace(tzinfo=tz)
            else:
                win_e = datetime.combine(day, w_end).replace(tzinfo=tz)
            # Step in UTC so DST transitions never create duplicate/skipped slots.
            cur, win_e_utc = to_utc(win_s), to_utc(win_e)
            while cur + duration <= win_e_utc:
                s, e = cur, cur + duration
                cur += step
                if s < earliest or s > latest:
                    continue
                if range_start and s < range_start or range_end and e > range_end:
                    continue
                if not is_free(s, e, busy_list, buffer_before=av.buffer_before,
                               buffer_after=av.buffer_after):
                    continue
                slots.append(Slot(s, e))
                if limit and len(slots) >= limit:
                    return slots
        day += timedelta(days=1)
    return slots


def group_by_local_date(slots: Iterable[Slot], tz: str) -> Dict[str, List[Slot]]:
    """Group slots by calendar date in the viewer's time zone (handy for UIs)."""
    out: Dict[str, List[Slot]] = {}
    z = ZoneInfo(tz)
    for slot in slots:
        out.setdefault(slot.start.astimezone(z).date().isoformat(), []).append(slot)
    return out
