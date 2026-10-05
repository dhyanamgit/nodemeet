""""Add to calendar" links for Google Calendar and Outlook (no API keys needed)."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional
from urllib.parse import urlencode

from ..models import Booking, to_utc


def _g(dt: datetime) -> str:
    return to_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def _o(dt: datetime) -> str:
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def google_calendar_link(title: str, start: datetime, end: datetime, *,
                         details: str = "", location: str = "") -> str:
    q = {"action": "TEMPLATE", "text": title, "dates": f"{_g(start)}/{_g(end)}",
         "details": details, "location": location}
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


def outlook_calendar_link(title: str, start: datetime, end: datetime, *,
                          details: str = "", location: str = "", office365: bool = False) -> str:
    host = "outlook.office.com" if office365 else "outlook.live.com"
    q = {"path": "/calendar/action/compose", "rru": "addevent", "subject": title,
         "startdt": _o(start), "enddt": _o(end), "body": details, "location": location}
    return f"https://{host}/calendar/0/deeplink/compose?" + urlencode(q)


def calendar_links(booking: Booking, *, join_url: Optional[str] = None,
                   details: Optional[str] = None) -> Dict[str, str]:
    body = details if details is not None else booking.notes
    if join_url:
        body = f"{body}\n\nJoin: {join_url}".strip()
    loc = join_url or ""
    return {
        "google": google_calendar_link(booking.title, booking.start, booking.end,
                                       details=body, location=loc),
        "outlook": outlook_calendar_link(booking.title, booking.start, booking.end,
                                         details=body, location=loc),
        "office365": outlook_calendar_link(booking.title, booking.start, booking.end,
                                           details=body, location=loc, office365=True),
    }
