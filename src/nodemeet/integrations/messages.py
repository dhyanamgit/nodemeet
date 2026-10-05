"""Turn nodemeet events into short human messages (used by chat and push integrations)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

COLORS = {"good": "#22c55e", "bad": "#ef4444", "warn": "#f59e0b", "info": "#6366f1"}


def _when(value: Any, tz: Optional[str] = None) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if tz:
            dt = dt.astimezone(ZoneInfo(tz))
        return dt.strftime("%a %d %b, %H:%M") + (f" ({tz})" if tz else " UTC")
    except Exception:  # noqa: BLE001
        return str(value)


def describe(event: str, data: Dict[str, Any], *, base_url: str = "",
             timezone: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """-> {"title", "text", "url", "color", "fields": {...}} or None for events we don't narrate."""
    b = data.get("booking") or {}
    who = b.get("attendee_name") or data.get("name") or data.get("user_id") or "Someone"
    when = _when(b.get("start"), timezone) if b.get("start") else ""
    title_of = b.get("title") or "Meeting"
    manage = f"{base_url}/book/manage/{b['id']}" if b.get("id") and base_url else None
    room = data.get("room") if isinstance(data.get("room"), str) else (data.get("room") or {}).get("id")
    fields = {k: v for k, v in (("When", when), ("Who", b.get("attendee_email")), ("Host", b.get("host_id"))) if v}
    if event == "booking.created":
        return {"title": f"New booking: {title_of}", "text": f"{who} booked {when}", "url": manage,
                "color": COLORS["good"], "fields": fields}
    if event == "booking.rescheduled":
        return {"title": f"Rescheduled: {title_of}", "text": f"{who} moved to {when}", "url": manage,
                "color": COLORS["warn"], "fields": fields}
    if event == "booking.cancelled":
        reason = b.get("cancel_reason")
        return {"title": f"Cancelled: {title_of}", "text": f"{who} on {when}" + (f" ({reason})" if reason else ""),
                "url": manage, "color": COLORS["bad"], "fields": fields}
    if event == "booking.paid":
        pay = (b.get("metadata") or {}).get("payment") or {}
        amount = pay.get("amount_paid") or pay.get("amount")
        money = f" {amount / 100:.2f} {pay.get('currency', '')}".rstrip() if isinstance(amount, int) else ""
        return {"title": "Payment received", "text": f"{who} paid{money} for {when}", "url": manage,
                "color": COLORS["good"], "fields": fields}
    if event == "booking.reminder":
        return {"title": f"Starting soon: {title_of}", "text": f"{when} with {who}", "url": manage,
                "color": COLORS["info"], "fields": fields}
    if event == "participant.waiting":
        return {"title": "Someone is waiting", "text": f"{who} is in the waiting room of {room}",
                "url": f"{base_url}/r/{room}" if base_url and room else None, "color": COLORS["warn"], "fields": {}}
    if event == "participant.blocked_attempt":
        return {"title": "Blocked person tried to rejoin", "text": f"{who} tried to rejoin {room}",
                "url": None, "color": COLORS["bad"], "fields": {}}
    if event == "recording.ready":
        return {"title": "Recording ready", "text": f"Recording of {room} is ready", "url": data.get("url"),
                "color": COLORS["good"], "fields": {}}
    if event == "room.created":
        return {"title": "Room created", "text": f"{data.get('name') or data.get('id')}", "url": None,
                "color": COLORS["info"], "fields": {}}
    if event == "participant.joined":
        return {"title": "Joined", "text": f"{who} joined {room}", "url": None, "color": COLORS["info"], "fields": {}}
    if event == "poll.closed":
        return {"title": "Poll closed", "text": str(data.get("question") or ""), "url": None,
                "color": COLORS["info"], "fields": {}}
    return None
