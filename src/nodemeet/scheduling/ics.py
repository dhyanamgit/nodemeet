"""RFC 5545 .ics generation (no third-party dependency)."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from .._version import __version__
from ..models import Booking, BookingStatus, to_utc, utcnow


def _escape(text: str) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545 section 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: List[str] = []
    while raw:
        limit = 75 if not parts else 74
        chunk = raw[:limit]
        while True:  # don't split a multi-byte character
            try:
                text = chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        parts.append(text)
        raw = raw[len(chunk):]
    return "\r\n ".join(parts)


def _param(text: str) -> str:
    """Parameter values (e.g. CN) are DQUOTE-wrapped, not backslash-escaped."""
    clean = str(text).replace('"', "'").replace("\r", " ").replace("\n", " ")
    return f'"{clean}"' if any(c in clean for c in ',;:') else clean


def _dt(value: datetime) -> str:
    return to_utc(value).strftime("%Y%m%dT%H%M%SZ")


def build_ics(booking: Booking, *, organizer_email: str = "", organizer_name: str = "",
              url: Optional[str] = None, description: Optional[str] = None,
              method: Optional[str] = None, alarms_minutes: Iterable[int] = (15,),
              uid_domain: str = "nodemeet") -> str:
    """Build a calendar invite for ``booking``.

    ``method`` defaults to ``REQUEST`` for active bookings and ``CANCEL`` for
    cancelled ones, so the same UID/SEQUENCE updates the attendee's calendar.
    """
    cancelled = booking.status == BookingStatus.CANCELLED
    method = method or ("CANCEL" if cancelled else "REQUEST")
    desc = description if description is not None else booking.notes
    if url:
        desc = f"{desc}\n\nJoin: {url}".strip()
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        f"PRODID:-//nodemeet//nodemeet {__version__}//EN",
        "CALSCALE:GREGORIAN", f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{booking.id}@{uid_domain}",
        f"SEQUENCE:{booking.sequence}",
        f"DTSTAMP:{_dt(utcnow())}",
        f"DTSTART:{_dt(booking.start)}",
        f"DTEND:{_dt(booking.end)}",
        f"SUMMARY:{_escape(booking.title)}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{_escape(desc)}")
    if url:
        lines += [f"LOCATION:{_escape(url)}", f"URL:{url}"]
    if organizer_email:
        cn = f";CN={_param(organizer_name)}" if organizer_name else ""
        lines.append(f"ORGANIZER{cn}:mailto:{organizer_email}")
    if booking.attendee_email:
        cn = f";CN={_param(booking.attendee_name)}" if booking.attendee_name else ""
        lines.append(f"ATTENDEE{cn};ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{booking.attendee_email}")
    if not cancelled:
        for minutes in alarms_minutes:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(booking.title)}",
                      f"TRIGGER:-PT{int(minutes)}M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
