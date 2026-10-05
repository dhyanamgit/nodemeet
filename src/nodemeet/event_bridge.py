"""Turns everything that happens inside nodemeet into webhook events (see nodemeet.events).

Wired automatically by NodeMeet. Three sources:
* hooks (join/leave/admit/moderation/roles/hands/reactions) -> meeting & participant events,
  plus booking attendance (host joined, attendee joined, completed, no-show);
* every client message that succeeded (polls, Q&A, whiteboard, breakouts, captions,
  recording, screen share...) -> collaboration events;
* admin REST calls (branding, roles, API keys, calendars, SSO failures) -> admin events.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .server import NodeMeet

log = logging.getLogger("nodemeet.events")

MODERATION_EVENTS = {
    "mute": "participant.muted", "mute-all": "participant.muted", "kick": "participant.kicked",
    "ban": "participant.banned", "block": "participant.banned", "deny": "participant.denied",
    "lock": "room.locked", "unlock": "room.unlocked", "end": "meeting.ended_by_host",
    "unban": "participant.unbanned",
}
_SKIP_KEYS = {"type", "token", "points", "strokes", "audio", "data", "sdp", "candidate"}


def who(p: Any) -> Dict[str, Any]:
    if p is None:
        return {}
    return {"user_id": p.user_id, "name": p.name, "role": p.role, "peer_id": p.peer_id}


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if k in _SKIP_KEYS:
            continue
        if isinstance(v, str):
            v = v[:2000]
        elif isinstance(v, (list, dict)) and len(str(v)) > 4000:
            continue
        out[k] = v
    return out


class EventBridge:
    def __init__(self, meet: "NodeMeet") -> None:
        self.meet = meet
        self.meetings: Dict[str, Dict[str, Any]] = {}  # room id -> live stats
        h = meet.hooks
        h.on("on_join", self.on_join)
        h.on("on_leave", self.on_leave)
        h.on("on_room_closed", self.on_room_closed)
        h.on("on_admitted", self.on_admitted)
        h.on("on_moderation", self.on_moderation)
        h.on("on_role_changed", self.on_role_changed)
        h.on("on_hand_raised", self.on_hand)
        h.on("on_reaction", self.on_reaction)
        meet.bookings.listeners.append(self.on_booking)

    async def emit(self, event: str, data: Dict[str, Any], room: Any = None) -> None:
        tenant = getattr(getattr(room, "config", None), "tenant_id", None)
        if room is not None:
            data = {"room": room.id, **data}
        await self.meet.emit_webhook(event, data, tenant_id=tenant)

    # -- meetings & attendance ----------------------------------------------------------------
    async def on_booking(self, event: str, booking: Any, extra: Dict[str, Any]) -> None:
        if event == "booking.created":
            await self.meet.storage.put_record("booking_room", booking.room, {"booking_id": booking.id})

    async def _booking_for(self, room_id: str) -> Any:
        try:
            ref = await self.meet.storage.get_record("booking_room", room_id)
        except NotImplementedError:
            return None
        return await self.meet.storage.get_booking(ref["booking_id"]) if ref else None

    async def on_join(self, room: Any, p: Any) -> None:
        stats = self.meetings.get(room.id)
        if stats is None:
            stats = self.meetings[room.id] = {"started_at": time.time(), "peak": 0, "attendees": {}, "open": {}}
            await self.emit("meeting.started", {"started_at": stats["started_at"], "first": who(p),
                                                "name": room.config.name}, room)
        stats["peak"] = max(stats["peak"], len(room.participants))
        stats["attendees"].setdefault(p.user_id, {"name": p.name, "role": p.role, "joined_at": time.time()})
        stats.setdefault("open", {})[p.peer_id] = (p.user_id, getattr(p, "joined_at", None) or time.time())
        booking = await self._booking_for(room.id)
        if booking is None:
            return
        side = "host" if (p.user_id == booking.host_id or p.role == "host") else \
            ("attendee" if p.user_id.lower() == booking.attendee_email.lower() else None)
        if side is None:
            return
        att = dict(booking.metadata.get("attendance") or {})
        if side in att:
            return
        att[side] = time.time()
        booking.metadata["attendance"] = att
        await self.meet.storage.save_booking(booking)
        payload = {"booking": booking.to_dict(include_secrets=False), "participant": who(p)}
        await self.meet.emit_webhook(f"booking.{side}_joined", payload, tenant_id=booking.tenant_id)
        if {"host", "attendee"} <= set(att):
            await self.meet.emit_webhook("booking.completed", payload, tenant_id=booking.tenant_id)

    @staticmethod
    def _close_session(stats: Dict[str, Any], peer_id: str, now: float) -> None:
        """Add one connection's time to its person (each tab/device counted once, when it leaves)."""
        entry = stats.get("open", {}).pop(peer_id, None)
        if entry is None:
            return
        user_id, started = entry
        a = stats["attendees"].get(user_id)
        if a is not None:
            a["seconds"] = a.get("seconds", 0) + max(0, round(now - started))

    async def on_leave(self, room: Any, p: Any) -> None:
        stats = self.meetings.get(room.id)
        if stats:
            self._close_session(stats, p.peer_id, time.time())

    async def on_room_closed(self, room: Any) -> None:
        stats = self.meetings.pop(room.id, None)
        if not stats:
            return
        ended = time.time()
        # The room closes *before* the last person's on_leave runs, so count whoever is still open here
        # (0.9.1 gave the last person out 0 minutes).
        for peer_id in list(stats.get("open", {})):
            self._close_session(stats, peer_id, ended)
        await self.emit("meeting.ended", {
            "started_at": stats["started_at"], "ended_at": ended,
            "duration_seconds": round(ended - stats["started_at"]), "peak_participants": stats["peak"],
            "attendee_count": len(stats["attendees"]),
            "attendees": [{"user_id": u, **a} for u, a in stats["attendees"].items()]}, room)

    async def on_admitted(self, room: Any, p: Any, by: Any = None) -> None:
        await self.emit("participant.admitted", {"participant": who(p), "by": who(by) if hasattr(by, "user_id") else by}, room)

    async def on_moderation(self, room: Any, actor: Any, action: str, target: Any, data: Any) -> None:
        payload = {"action": action, "by": who(actor), "target": who(target), "details": _clean(dict(data or {}))}
        await self.emit("participant.moderated", payload, room)
        event = MODERATION_EVENTS.get(action)
        if event:
            await self.emit(event, payload, room)

    async def on_role_changed(self, room: Any, p: Any, old: Any) -> None:
        await self.emit("participant.role_changed", {"participant": who(p), "old_role": str(old), "new_role": p.role}, room)

    async def on_hand(self, room: Any, p: Any, raised: bool) -> None:
        await self.emit("participant.hand_raised" if raised else "participant.hand_lowered", {"participant": who(p)}, room)

    async def on_reaction(self, room: Any, p: Any, emoji: str) -> None:
        await self.emit("participant.reaction", {"participant": who(p), "emoji": emoji}, room)

    # -- client messages ------------------------------------------------------------------------
    async def after_message(self, session: Any, kind: str, data: Dict[str, Any], before: Tuple[Any, ...]) -> None:
        me, room = session.me, session.room
        if me is None or room is None:
            return
        base = {"by": who(me)}
        k = kind.replace("-", "_")
        ev: List[Tuple[str, Dict[str, Any]]] = []
        if k == "state":
            screen, audio, video = before
            if screen != me.screen_sharing:
                ev.append(("screen_share.started" if me.screen_sharing else "screen_share.stopped", {}))
            if audio != me.audio_muted or video != me.video_muted:
                ev.append(("participant.media_changed", {"audio_muted": me.audio_muted, "video_muted": me.video_muted}))
        elif k == "rename":
            ev.append(("participant.renamed", {"name": me.name}))
        elif k in ("chat_delete", "chat_pin"):
            ev.append(("chat.deleted" if k == "chat_delete" else "chat.pinned", _clean(data)))
        elif k in ("poll_create", "poll_vote", "poll_delete"):
            ev.append(({"poll_create": "poll.created", "poll_vote": "poll.voted", "poll_delete": "poll.deleted"}[k], _clean(data)))
        elif k in ("qa_ask", "qa_upvote", "qa_answer"):
            ev.append(({"qa_ask": "qa.asked", "qa_upvote": "qa.upvoted", "qa_answer": "qa.answered"}[k], _clean(data)))
        elif k == "board_clear":
            ev.append(("whiteboard.cleared", {}))
        elif k == "board_draw":
            ev.append(("whiteboard.drawn", {}))
        elif k == "recording" and data.get("action") == "stop":
            ev.append(("recording.stopped", {}))
        elif k == "captions":
            on = bool(data.get("enabled", data.get("on", data.get("action") == "start")))
            ev.append(("captions.started" if on else "captions.stopped", {}))
        elif k == "caption_text" and data.get("final", True):
            ev.append(("caption.line", {"text": str(data.get("text", ""))[:1000]}))
        elif k == "breakout":
            name = {"set": "configured", "open": "opened", "close": "closed", "move": "moved",
                    "announce": "announced"}.get(str(data.get("action")))
            if name:
                ev.append((f"breakout.{name}", _clean(data)))
        for event, extra in ev:
            await self.emit(event, {**base, **extra}, room)

    # -- admin REST calls -------------------------------------------------------------------------
    ROUTES: List[Tuple[str, "re.Pattern[str]", str]] = [
        ("PUT|PATCH", re.compile(r"^/api/branding$"), "branding.updated"),
        ("POST", re.compile(r"^/api/keys$"), "api_key.created"),
        ("DELETE", re.compile(r"^/api/keys/(?P<id>[^/]+)$"), "api_key.revoked"),
        ("POST|PATCH", re.compile(r"^/api/roles(?:/(?P<name>[^/]+))?(?:/reset)?$"), "role.saved"),
        ("DELETE", re.compile(r"^/api/roles/(?P<name>[^/]+)$"), "role.deleted"),
        ("DELETE", re.compile(r"^/api/hosts/(?P<host_id>[^/]+)/calendars/(?P<provider>[^/]+)$"), "calendar.disconnected"),
    ]

    async def after_http(self, req: Any, resp: Any) -> None:
        path = req.path or "/"
        if path.startswith("/sso/") and resp.status >= 400 and ("/callback" in path or "/acs" in path):
            await self.meet.emit_webhook("sso.failed", {"provider": path.split("/")[2], "status": resp.status,
                                                        "error": _resp_error(resp)})
            return
        if resp.status >= 300 or req.method in ("GET", "HEAD", "OPTIONS"):
            return
        for methods, rx, event in self.ROUTES:
            m = rx.match(path)
            if m and req.method in methods.split("|"):
                data: Dict[str, Any] = {k: v for k, v in m.groupdict().items() if v}
                body = _resp_json(resp)
                if isinstance(body, dict):
                    body.pop("key", None)  # never leak a freshly minted API key
                    body.pop("secret", None)
                    data["result"] = body
                await self.meet.emit_webhook(event, data)
                return


def _resp_json(resp: Any) -> Any:
    import json
    try:
        return json.loads(resp.body or b"null")
    except Exception:  # noqa: BLE001
        return None


def _resp_error(resp: Any) -> str:
    body = _resp_json(resp)
    if isinstance(body, dict):
        err = body.get("error")
        return str(err.get("code") if isinstance(err, dict) else err or body.get("code") or "")
    return ""
