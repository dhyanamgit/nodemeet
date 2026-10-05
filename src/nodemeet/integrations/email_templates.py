"""Default booking email copy. Subclass :class:`EmailTemplates` to rebrand."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from ..models import Booking

Rendered = Tuple[str, str, str]  # subject, text, html


@dataclass
class EmailContext:
    join_url: str = ""
    manage_url: str = ""
    host_name: str = ""
    timezone: str = "UTC"
    calendar_links: Dict[str, str] = field(default_factory=dict)
    reason: str = ""
    minutes_before: int = 0
    old_start: Optional[datetime] = None


class EmailTemplates:
    brand = "nodemeet"
    accent = "#4f46e5"
    logo_url = ""
    footer = ""
    font = "system-ui,sans-serif"

    def with_branding(self, b: Optional[Dict[str, Any]]) -> "EmailTemplates":
        """A copy themed with a (resolved) branding dict: name, colours, logo, footer."""
        if not b:
            return self
        import copy as _copy
        t = _copy.copy(self)
        email = b.get("email") or {}
        t.brand = email.get("from_name") or b.get("name") or self.brand
        t.accent = email.get("accent") or (b.get("light_colors") or {}).get("primary") or self.accent
        t.logo_url = email.get("logo_url") or b.get("logo_url") or self.logo_url
        t.footer = email.get("footer") or self.footer
        t.font = (b.get("font_family") or self.font).replace("'", "")
        return t

    def when(self, dt: datetime, tz: str) -> str:
        local = dt.astimezone(ZoneInfo(tz))
        return local.strftime("%A, %d %B %Y at %H:%M ") + tz

    def _html(self, heading: str, lines: list, button: Optional[Tuple[str, str]] = None) -> str:
        body = "".join(f"<p style='margin:0 0 10px'>{line}</p>" for line in lines)
        btn = ""
        if button:
            btn = (f"<p><a href='{html.escape(button[1])}' style='display:inline-block;"
                   f"background:{self.accent};color:#fff;padding:10px 18px;border-radius:8px;"
                   f"text-decoration:none'>{html.escape(button[0])}</a></p>")
        logo = (f"<img src='{html.escape(self.logo_url)}' alt='{html.escape(self.brand)}' "
                f"style='max-height:40px;margin-bottom:16px'>" if self.logo_url else "")
        footer = html.escape(self.footer) if self.footer else f"Sent by {html.escape(self.brand)}"
        return (f"<div style=\"font-family:{html.escape(self.font)};max-width:560px;margin:auto;"
                f"padding:24px;color:#111\">{logo}<h2 style='margin-top:0'>{html.escape(heading)}</h2>"
                f"{body}{btn}<p style='color:#888;font-size:12px'>{footer}</p></div>")

    def _links(self, ctx: EmailContext) -> Tuple[str, list]:
        text, rows = "", []
        if ctx.calendar_links:
            text = "\nAdd to calendar:\n" + "\n".join(f"  {k}: {v}" for k, v in ctx.calendar_links.items())
            rows = [" · ".join(f"<a href='{html.escape(v)}'>{html.escape(k.title())}</a>"
                               for k, v in ctx.calendar_links.items())]
        if ctx.manage_url:
            text += f"\nReschedule or cancel: {ctx.manage_url}"
            rows.append(f"<a href='{html.escape(ctx.manage_url)}'>Reschedule or cancel</a>")
        return text, rows

    def confirmation(self, b: Booking, ctx: EmailContext) -> Rendered:
        when = self.when(b.start, ctx.timezone)
        subject = f"Confirmed: {b.title} on {when}"
        extra, rows = self._links(ctx)
        text = (f"Hi {b.attendee_name},\n\nYour meeting '{b.title}'"
                f"{' with ' + ctx.host_name if ctx.host_name else ''} is booked for {when} "
                f"({b.duration_minutes} min).\n\nJoin: {ctx.join_url}\n{extra}\n")
        lines = [f"Hi {html.escape(b.attendee_name)},",
                 f"<b>{html.escape(b.title)}</b> is booked for <b>{html.escape(when)}</b> "
                 f"({b.duration_minutes} min)."] + rows
        return subject, text, self._html("You're booked", lines, ("Join meeting", ctx.join_url))

    def reminder(self, b: Booking, ctx: EmailContext) -> Rendered:
        when = self.when(b.start, ctx.timezone)
        subject = f"Reminder: {b.title} starts {self._in(ctx.minutes_before)}"
        text = f"Hi {b.attendee_name},\n\n'{b.title}' starts at {when}.\n\nJoin: {ctx.join_url}\n"
        lines = [f"Hi {html.escape(b.attendee_name)},",
                 f"<b>{html.escape(b.title)}</b> starts at <b>{html.escape(when)}</b>."]
        return subject, text, self._html("Starting soon", lines, ("Join meeting", ctx.join_url))

    def cancellation(self, b: Booking, ctx: EmailContext) -> Rendered:
        when = self.when(b.start, ctx.timezone)
        subject = f"Cancelled: {b.title} on {when}"
        reason = f"\nReason: {ctx.reason}\n" if ctx.reason else ""
        text = f"Hi {b.attendee_name},\n\n'{b.title}' on {when} has been cancelled.\n{reason}"
        lines = [f"Hi {html.escape(b.attendee_name)},",
                 f"<b>{html.escape(b.title)}</b> on {html.escape(when)} has been cancelled."]
        if ctx.reason:
            lines.append(f"Reason: {html.escape(ctx.reason)}")
        return subject, text, self._html("Meeting cancelled", lines)

    def rescheduled(self, b: Booking, ctx: EmailContext) -> Rendered:
        when = self.when(b.start, ctx.timezone)
        old = self.when(ctx.old_start, ctx.timezone) if ctx.old_start else "the previous time"
        subject = f"Rescheduled: {b.title} is now {when}"
        extra, rows = self._links(ctx)
        text = (f"Hi {b.attendee_name},\n\n'{b.title}' moved from {old} to {when}.\n\n"
                f"Join: {ctx.join_url}\n{extra}\n")
        lines = [f"Hi {html.escape(b.attendee_name)},",
                 f"<b>{html.escape(b.title)}</b> moved from {html.escape(old)} to "
                 f"<b>{html.escape(when)}</b>."] + rows
        return subject, text, self._html("Meeting rescheduled", lines, ("Join meeting", ctx.join_url))

    @staticmethod
    def _in(minutes: int) -> str:
        if minutes >= 1440 and minutes % 1440 == 0:
            d = minutes // 1440
            return f"in {d} day{'s' if d > 1 else ''}"
        if minutes >= 60 and minutes % 60 == 0:
            h = minutes // 60
            return f"in {h} hour{'s' if h > 1 else ''}"
        return f"in {minutes} minutes"
