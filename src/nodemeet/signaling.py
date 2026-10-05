"""WebSocket signaling: join, lobby, WebRTC relay, chat, presence, moderation, SFU.

Wire protocol (JSON, ``type`` field). Client -> server:

    join {token, room?, name?}            first message (or ?token= on the URL)
    signal {to, data}                     p2p SDP/ICE relay
    chat {text, to?}                      to = peer_id for a private message
    chat-delete {id} / chat-pin {id|null}
    reaction {emoji}
    state {audio_muted?, video_muted?, screen_sharing?}
    raise-hand {raised} / rename {name}
    moderate {action, target?, ...}       see MODERATION below
    sfu-publish {sdp, sdpType} / sfu-subscribe {publisher, sdp, sdpType} / sfu-unsubscribe {publisher}
    ping / leave

Server -> client: welcome, lobby, lobby-update, admitted, denied, peer-joined, peer-left,
peer-updated, signal, chat, chat-deleted, chat-pinned, reaction, spotlight, topology,
publisher, sfu-*-answer, permissions, force-mute, force-video-off, force-stop-screen,
unmute-request, unmute-allowed, video-allowed, kicked, banned, room-closed, room-updated,
error, pong
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol

_BACKGROUND: "set[asyncio.Future[Any]]" = set()

from .exceptions import InvalidToken, JoinRejected, NodeMeetError, RoomFull
from .hooks import JoinContext
from .features import FeaturesMixin
from .models import ChatMessage
from .rooms import Participant, Room

if TYPE_CHECKING:
    from .server import NodeMeet

log = logging.getLogger("nodemeet.signaling")
MAX_CHAT = 4000
JOIN_TIMEOUT = 15.0
LOBBY_TIMEOUT = 15 * 60.0
LINK_RE = re.compile(r"(https?://|www\.)\S+", re.I)

# action -> permission needed (target actions need a target peer_id)
MODERATION: Dict[str, str] = {
    "mute": "moderate.mute", "mute-all": "moderate.mute_all",
    "ask-unmute": "moderate.ask_unmute", "allow-unmute": "moderate.ask_unmute",
    "video-off": "moderate.video_off", "allow-video": "moderate.video_off",
    "stop-screen": "moderate.stop_screen", "lower-hand": "moderate.lower_hands",
    "lower-all-hands": "moderate.lower_hands", "rename": "moderate.rename",
    "kick": "moderate.kick", "ban": "moderate.ban", "unban": "moderate.ban",
    "promote": "moderate.stage", "demote": "moderate.stage",
    "set-role": "moderate.assign_roles", "spotlight": "moderate.spotlight",
    "admit": "moderate.lobby", "deny": "moderate.lobby", "admit-all": "moderate.lobby",
    "admit-always": "moderate.lobby", "block": "moderate.ban",
    "allow": "moderate.lobby", "disallow": "moderate.lobby",
    "lobby-on": "moderate.lobby", "lobby-off": "moderate.lobby",
    "lock": "moderate.lock", "unlock": "moderate.lock", "lobby-message": "moderate.lobby",
    "lobby-mode": "moderate.lobby",
    "chat-on": "moderate.chat", "chat-off": "moderate.chat", "end": "moderate.end",
}
TARGETED = {"mute", "ask-unmute", "allow-unmute", "video-off", "allow-video", "stop-screen",
            "lower-hand", "rename", "kick", "ban", "promote", "demote", "set-role",
            "allow", "disallow"}


class WSTransport(Protocol):
    """What a web framework adapter must provide for one WebSocket."""

    query: Dict[str, str]
    headers: Dict[str, str]
    remote: Optional[str]

    @property
    def closed(self) -> bool: ...
    async def receive_text(self) -> Optional[str]: ...  # None once the socket is closed
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


def _redact(p: Dict[str, Any]) -> Dict[str, Any]:
    return {**p, "name": p.get("role_label") or "Participant", "user_id": "", "attributes": {}}


class Session(FeaturesMixin):
    def __init__(self, meet: "NodeMeet", ws: WSTransport) -> None:
        self.meet = meet
        self.ws = ws
        self.room: Optional[Room] = None
        self.me: Optional[Participant] = None
        self.waiting: Optional[Participant] = None  # in the lobby
        self._send_lock = asyncio.Lock()
        self._admission: Optional["asyncio.Future[bool]"] = None
        self._blocked = False
        self.device: Optional[str] = None

    # -- outgoing, filtered per recipient --------------------------------
    def _visible(self, p: Dict[str, Any]) -> bool:
        me = self.me
        return not p.get("hidden") or me is None or me.can("participants.see_hidden") \
            or p.get("peer_id") == me.peer_id

    def _shape(self, p: Dict[str, Any]) -> Dict[str, Any]:
        me = self.me
        if me is not None and not me.can("participants.list") and p.get("peer_id") != me.peer_id:
            return _redact(p)
        return p

    def _filter(self, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        me, kind = self.me, m.get("type")
        if me is None:
            return m
        if kind in ("chat", "chat-deleted", "chat-pinned") and not me.can("chat.read"):
            return None
        if kind in ("lobby-update",) and not me.can("moderate.lobby"):
            return None
        if kind == "ban-attempt" and not me.can("moderate.ban"):
            return None
        if isinstance(kind, str) and kind.startswith("_"):  # internal cluster state ops
            return None
        if kind == "poll" and bool(m.get("full")) != me.can("polls.results"):
            return None
        if kind == "breakouts" and bool(m.get("manager")) != me.can("breakout.manage"):
            return None
        if kind == "board" and not me.can("whiteboard.view"):
            return None
        if kind == "caption" and not me.can("captions.view"):
            return None
        if kind in ("peer-joined", "peer-updated"):
            if not self._visible(m["participant"]):
                return None
            return {**m, "participant": self._shape(m["participant"])}
        if kind in ("welcome", "topology", "room-updated"):
            m = dict(m)
            holder = m["room"] if kind in ("welcome", "room-updated") else m
            if kind in ("welcome", "room-updated"):
                holder = dict(holder)
                if not me.can("moderate.lobby"):
                    holder.pop("join_list", None)
                if not me.can("moderate.ban"):
                    holder.pop("banned", None)
                m["room"] = holder
            if "participants" in holder:
                holder = dict(holder, participants=[self._shape(p) for p in holder["participants"]
                                                    if self._visible(p)])
                if kind in ("welcome", "room-updated"):
                    m["room"] = holder
                else:
                    m = holder
        return m

    async def send(self, message: Dict[str, Any]) -> None:
        if self.ws.closed:
            return
        shaped = self._filter(message)
        if shaped is None:
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps(shaped, default=str))

    async def error(self, code: str, message: str, fatal: bool = False) -> None:
        self._errored = True
        await self.send({"type": "error", "code": code, "message": message})
        if fatal:
            await self.ws.close(4000)

    # -- lifecycle ---------------------------------------------------------
    async def _first_message(self) -> Dict[str, Any]:
        token = self.ws.query.get("token")
        if token:
            return {"type": "join", "token": token, "room": self.ws.query.get("room")}
        raw = await asyncio.wait_for(self.ws.receive_text(), JOIN_TIMEOUT)
        if raw is None:
            raise InvalidToken("connection closed before join")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("type") != "join":
            raise InvalidToken("expected a join message")
        return data

    async def run(self) -> None:
        try:
            joined = await self.join(await self._first_message())
        except (InvalidToken, JoinRejected, RoomFull, NodeMeetError, asyncio.TimeoutError,
                ValueError) as exc:
            code = getattr(exc, "code", None)
            await self.error(code if isinstance(code, str) else type(exc).__name__,
                             getattr(exc, "reason", None) or str(exc) or "join failed", fatal=True)
            return
        if not joined:
            return
        while True:
            raw = await self.ws.receive_text()
            if raw is None:
                break
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("message must be an object")
                await self.dispatch(data)
            except (json.JSONDecodeError, ValueError) as exc:
                await self.error("bad_request", str(exc) or "invalid message")
            except Exception as exc:  # noqa: BLE001
                log.exception("error handling %r", raw[:200])
                await self.error("server_error", str(exc))

    async def join(self, data: Dict[str, Any]) -> bool:
        meet = self.meet
        claims = meet.tokens.verify(str(data.get("token", "")))
        room_id = claims.room if claims.room != "*" else str(data.get("room") or "")
        if not room_id:
            raise InvalidToken("room is required for wildcard tokens")
        if data.get("room") and data["room"] != room_id:
            raise InvalidToken("token is for a different room")
        room = await meet.rooms.open(room_id, tenant_id=claims.tenant)
        cfg = room.config
        if cfg.tenant_id and claims.tenant != cfg.tenant_id:
            raise InvalidToken("token was not issued for this room's tenant")
        self.device = str(data.get("device") or "")[:64] or None
        if cfg.is_banned(claims.user_id, self.device, self.ws.remote):
            await self._ban_attempt(room, claims, str(data.get("name") or claims.name or claims.user_id))
            raise JoinRejected("You were removed from this meeting and can't rejoin. "
                               "Ask the host if you think this is a mistake.", code="banned")
        if claims.meta.get("booking_id"):
            booking = await meet.storage.get_booking(str(claims.meta["booking_id"]))
            if booking is not None and booking.status.value == "cancelled":
                raise JoinRejected("this booking was cancelled", code="booking_cancelled")
            if booking is not None and booking.awaiting_payment:
                raise JoinRejected("complete the payment to join this meeting", code="payment_required")
        role_name = claims.role or cfg.default_role or meet.roles.default_role
        try:
            eff = await meet.roles.resolve(role_name, cfg.tenant_id, room_overrides=cfg.role_overrides,
                                           grant=claims.grant, revoke=claims.revoke)
        except ValueError as exc:
            raise JoinRejected(str(exc), code="unknown_role") from None
        name = str(data.get("name") or claims.name or claims.user_id)[:80]
        if data.get("name") and not eff.can("profile.rename") and claims.name:
            name = claims.name[:80]
        ctx = JoinContext(room_id=room_id, claims=claims, name=name, role=eff.name,
                          remote=self.ws.remote, headers=dict(self.ws.headers),
                          participant_count=len(room), permissions=set(eff.permissions),
                          attributes={**eff.attributes, "_grant": list(claims.grant),
                                      "_revoke": list(claims.revoke)})
        await meet.hooks.run_before_join(ctx)
        if ctx.role != eff.name:  # a hook switched the role
            eff = await meet.roles.resolve(ctx.role, cfg.tenant_id, room_overrides=cfg.role_overrides,
                                           grant=claims.grant, revoke=claims.revoke)
            ctx.permissions, ctx.attributes = set(eff.permissions), {**eff.attributes, **{
                k: v for k, v in ctx.attributes.items() if k.startswith("_")}}
        if "room.join" not in ctx.permissions:
            raise JoinRejected("your role is not allowed to join", code="forbidden")
        me = Participant(user_id=claims.user_id, name=ctx.name, role=ctx.role,
                         permissions=set(ctx.permissions), attributes=dict(ctx.attributes),
                         meta=dict(claims.meta), send=self.send, close=self.force_close)
        me.attributes["_device"], me.attributes["_ip"] = self.device, self.ws.remote
        me.audio_muted = bool(me.attributes.get("auto_mute"))
        me.video_muted = bool(me.attributes.get("auto_video_off"))
        async with room.lock:
            self._check_access(room, me)
        # Waiting room: hosts (room.bypass_lobby), people on the join list and people
        # who booked this meeting go straight in; everyone else waits to be admitted.
        on_list = cfg.on_join_list(claims.user_id) or bool(claims.meta.get("booking_id"))
        host_here = any(p.can("moderate.lobby") for p in room.participants.values())
        if cfg.lobby_mode == "until_host" and host_here:
            on_list = True  # the host is here: no need to wait
        if cfg.lobby and not on_list and not me.can("room.bypass_lobby"):
            if not await self._wait_in_lobby(room, me):
                return False
        async with room.lock:
            self._check_access(room, me)  # again: state may have changed while waiting
            changed = await meet.rooms.add(room, me)
            self.room, self.me = room, me
        await self._welcome(room, me)
        await self.send_features()
        if cfg.lobby_mode == "until_host" and me.can("moderate.lobby"):
            for waiting in list(room.lobby.values()):  # the host arrived: let everyone in
                waiting.decide(True)
        await room.broadcast({"type": "peer-joined", "participant": me.to_public(room)},
                             exclude=me.peer_id)
        if changed:
            await self.topology_changed(room)
        await meet.hooks.emit("on_join", room, me)
        await meet.emit_webhook("participant.joined", {"room": room.id, **me.to_public(room)},
                                tenant_id=cfg.tenant_id)
        return True

    def _check_access(self, room: Room, me: Participant) -> None:
        if room.config.locked and not me.can("room.join_locked"):
            raise JoinRejected("this room is locked", code="locked")
        if not me.can("room.join_full"):
            self.meet.rooms.check_capacity(room)

    async def _welcome(self, room: Room, me: Participant) -> None:
        meet = self.meet
        roles = []
        if me.can("moderate.assign_roles"):
            roles = [r.public() for r in (await meet.roles.all(room.config.tenant_id)).values()]
        history = [m.to_dict() for m in room.chat] if me.can("chat.history") else []
        await self.send({
            "type": "welcome", "peer_id": me.peer_id, "room": room.snapshot(),
            "you": me.to_public(room), "permissions": sorted(me.permissions),
            "topology": room.topology, "ice_servers": meet.ice_servers,
            "publishers": meet.sfu.publishers(room.id) if room.topology == "sfu" else [],
            "chat": history, "roles": roles,
            "lobby": self.lobby_list(room) if me.can("moderate.lobby") else [],
            "branding": await meet.branding_for(room.config),
        })

    @staticmethod
    def lobby_list(room: Room) -> List[Dict[str, Any]]:
        return [s.waiting.to_public(room) for s in room.lobby.values() if s.waiting is not None]

    async def _wait_in_lobby(self, room: Room, me: Participant) -> bool:
        self.waiting = me
        self._admission = asyncio.get_running_loop().create_future()
        room.lobby[me.peer_id] = self
        branding = await self.meet.branding_for(room.config)
        await self.ws.send_text(json.dumps({
            "type": "lobby", "peer_id": me.peer_id, "position": len(room.lobby),
            "message": room.config.lobby_message or branding.get("lobby_message") or "",
            "mode": room.config.lobby_mode}))
        await self.meet.hooks.emit("on_lobby", room, me)
        await self.meet.emit_webhook("participant.waiting", {
            "room": room.id, "user_id": me.user_id, "name": me.name,
            "owner": (room.config.metadata or {}).get("owner"),
            "notify": (room.config.metadata or {}).get("notify") or []}, tenant_id=room.config.tenant_id)
        await room.broadcast({"type": "lobby-update", "lobby": self.lobby_list(room)})
        await notify_lobby(room)
        try:
            while True:
                recv = asyncio.ensure_future(self.ws.receive_text())
                done, _ = await asyncio.wait({recv, self._admission}, timeout=LOBBY_TIMEOUT,
                                             return_when=asyncio.FIRST_COMPLETED)
                if self._admission in done:
                    recv.cancel()
                    admitted = self._admission.result()
                    kind = "admitted" if admitted else ("banned" if self._blocked else "denied")
                    await self.ws.send_text(json.dumps({"type": kind}))
                    if not admitted:
                        await self.ws.close(4003)
                    return admitted
                if not done:  # timed out
                    recv.cancel()
                    await self.error("lobby_timeout", "nobody admitted you in time", fatal=True)
                    return False
                if recv.result() is None:  # left while waiting
                    return False
        finally:
            room.lobby.pop(me.peer_id, None)
            self.waiting = None
            await room.broadcast({"type": "lobby-update", "lobby": self.lobby_list(room)})
            await notify_lobby(room)

    def decide(self, admitted: bool, *, blocked: bool = False) -> None:
        self._blocked = blocked
        if self._admission is not None and not self._admission.done():
            self._admission.set_result(admitted)

    async def _ban_attempt(self, room: Room, claims: Any, name: str) -> None:
        """A blocked person tried to come back: refuse, and tell moderators (at most once a minute)."""
        now = time.time()
        if now - room.ban_attempts.get(claims.user_id, 0) < 60:
            return
        room.ban_attempts[claims.user_id] = now
        info = room.config.banned_info.get(claims.user_id, {})
        event = {"user_id": claims.user_id, "name": info.get("name") or name, "at": now,
                 "banned_by": info.get("by"), "ip": self.ws.remote}
        await room.broadcast({"type": "ban-attempt", **{k: v for k, v in event.items() if k != "ip"}})
        await self.meet.hooks.emit("on_blocked_attempt", room, event)
        await self.meet.emit_webhook("participant.blocked_attempt", {"room": room.id, **event},
                                     tenant_id=room.config.tenant_id)

    async def force_close(self, code: int = 1000) -> None:
        """Remove this person now and close their socket in the background.

        Closing a WebSocket waits for the other side to answer; a client that never answers
        (or a slow phone) must not stall the moderator who kicked it or the meeting's end."""
        await self.cleanup()

        async def bye() -> None:
            try:
                await asyncio.wait_for(self.ws.close(code), 5)
            except Exception:  # noqa: BLE001 - it's gone either way
                pass
        task = asyncio.ensure_future(bye())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    async def cleanup(self) -> None:
        room, me = self.room, self.me
        if room is None or me is None:
            return
        self.room = self.me = None
        await self.meet.sfu.remove_peer(room.id, me.peer_id)
        await self.meet.rooms.remove(room, me.peer_id)
        if room.spotlight == me.peer_id:
            room.spotlight = None
            await room.broadcast({"type": "spotlight", "peer_id": None})
        if room.participants:
            await room.broadcast({"type": "peer-left", "peer_id": me.peer_id})
            if room.recompute_topology(p2p_max=self.meet.rooms.p2p_max,
                                       sfu_available=self.meet.rooms.sfu_available):
                await self.topology_changed(room)
        if not room.local_participants():
            await self.meet.sfu.remove_room(room.id)
        await self.meet.hooks.emit("on_leave", room, me)
        await self.meet.emit_webhook("participant.left",
                                     {"room": room.id, "peer_id": me.peer_id,
                                      "user_id": me.user_id}, tenant_id=room.config.tenant_id)

    async def topology_changed(self, room: Room) -> None:
        if room.topology == "p2p":
            await self.meet.sfu.remove_room(room.id)
        if room.bus is not None:
            await room.bus.set_topology(room)
        await room.broadcast({"type": "topology", "topology": room.topology,
                              "participants": [p.to_public(room)
                                               for p in room.participants.values()]})

    # -- message handlers ----------------------------------------------------
    async def dispatch(self, data: Dict[str, Any]) -> None:
        kind = str(data.get("type", ""))
        handler = getattr(self, "on_" + kind.replace("-", "_"), None)
        if handler is None:
            await self.error("unknown_type", f"unknown message type {kind!r}")
            return
        me = self.me
        before = (me.screen_sharing, me.audio_muted, me.video_muted) if me else ()
        self._errored = False
        await handler(data)
        bridge = getattr(self.meet, "events", None)
        if bridge is not None and not self._errored and kind not in ("ping", "signal", "join", "leave") \
                and not kind.startswith("sfu-"):
            try:
                await bridge.after_message(self, kind, data, before)
            except Exception:  # noqa: BLE001 - events must never break a meeting
                log.exception("event bridge failed for %s", kind)

    async def forbid(self, what: str) -> None:
        await self.error("forbidden", f"your role does not allow: {what}")

    async def on_ping(self, data: Dict[str, Any]) -> None:
        await self.send({"type": "pong", "ts": data.get("ts")})

    async def on_leave(self, data: Dict[str, Any]) -> None:
        await self.force_close()

    async def on_signal(self, data: Dict[str, Any]) -> None:
        assert self.room and self.me
        target = str(data.get("to", ""))
        if not await self.room.send_to(target, {"type": "signal", "from": self.me.peer_id,
                                                "data": data.get("data")}):
            await self.error("unknown_peer", f"peer {target} is not in the room")

    async def on_chat(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        to = str(data.get("to") or "")
        need = "chat.private" if to else "chat.send"
        if not me.can(need):
            return await self.forbid(need)
        if not room.config.chat_enabled and not me.can("moderate.chat"):
            return await self.error("chat_disabled", "chat is turned off")
        text = str(data.get("text", "")).strip()[:MAX_CHAT]
        if not text:
            return
        if LINK_RE.search(text) and not me.can("chat.links"):
            return await self.forbid("chat.links")
        limit = me.attributes.get("chat_per_minute")
        if limit:
            now = time.time()
            me.chat_times = [t for t in me.chat_times if now - t < 60]
            if len(me.chat_times) >= int(limit):
                return await self.error("rate_limited", "you're sending messages too fast")
            me.chat_times.append(now)
        final = await self.meet.hooks.run_on_chat(room, me, text)
        if final is None:
            return
        msg = ChatMessage(room=room.id, peer_id=me.peer_id, user_id=me.user_id, name=me.name,
                          text=final)
        if to:
            target = room.participants.get(to)
            if target is None:
                return await self.error("unknown_peer", "no such participant")
            payload = {"type": "chat", "message": {**msg.to_dict(), "private": True, "to": to}}
            await room.send_to(to, payload)
            await self.send(payload)
            return
        room.chat.append(msg)
        await self.meet.storage.save_chat(msg)
        await room.broadcast({"type": "chat", "message": msg.to_dict()})
        await self.meet.emit_webhook("chat.message", msg.to_dict(), tenant_id=room.config.tenant_id)

    async def on_chat_delete(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        mid = str(data.get("id", ""))
        msg = next((m for m in room.chat if m.id == mid), None)
        if msg is None:
            return await self.error("not_found", "message not found")
        need = "chat.delete_own" if msg.peer_id == me.peer_id or msg.user_id == me.user_id \
            else "chat.delete_any"
        if not me.can(need) and not me.can("chat.delete_any"):
            return await self.forbid(need)
        room.chat.remove(msg)
        await self.meet.storage.delete_chat(room.id, mid)
        if room.pinned and room.pinned.get("id") == mid:
            room.pinned = None
            await room.broadcast({"type": "chat-pinned", "message": None})
        await room.broadcast({"type": "chat-deleted", "id": mid})

    async def on_chat_pin(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        if not me.can("chat.pin"):
            return await self.forbid("chat.pin")
        mid = data.get("id")
        msg = next((m for m in room.chat if m.id == mid), None) if mid else None
        room.pinned = msg.to_dict() if msg else None
        await room.broadcast({"type": "chat-pinned", "message": room.pinned})

    async def on_reaction(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        if not me.can("reactions.send"):
            return await self.forbid("reactions.send")
        emoji = str(data.get("emoji", ""))[:8]
        if emoji:
            await room.broadcast({"type": "reaction", "peer_id": me.peer_id, "emoji": emoji})
            await self.meet.hooks.emit("on_reaction", room, me, emoji)

    async def _updated(self, p: Participant) -> None:
        assert self.room
        await updated(self.meet, self.room, p)

    async def on_state(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        if "audio_muted" in data:
            want = bool(data["audio_muted"])
            if not want and not room.can_publish(me, "audio"):
                return await self.forbid("audio.publish")
            if not want and me.audio_locked and not me.can("audio.unmute_self"):
                return await self.error("locked", "a moderator muted you; ask to unmute")
            me.audio_muted = want
            if not want:
                me.audio_locked = False
        if "video_muted" in data:
            want = bool(data["video_muted"])
            if not want and not room.can_publish(me, "video"):
                return await self.forbid("video.publish")
            if not want and me.video_locked and not me.can("video.start_self"):
                return await self.error("locked", "a moderator turned off your camera")
            me.video_muted = want
            if not want:
                me.video_locked = False
        if "screen_sharing" in data:
            if data["screen_sharing"] and not room.can_publish(me, "screen"):
                return await self.forbid("screen.publish")
            me.screen_sharing = bool(data["screen_sharing"])
        await self._updated(me)

    async def on_raise_hand(self, data: Dict[str, Any]) -> None:
        me = self.me
        assert me and self.room
        if not me.can("hand.raise"):
            return await self.forbid("hand.raise")
        me.hand_raised = bool(data.get("raised", True))
        await self._updated(me)
        await self.meet.hooks.emit("on_hand_raised", self.room, me, me.hand_raised)

    async def on_rename(self, data: Dict[str, Any]) -> None:
        me = self.me
        assert me
        if not me.can("profile.rename"):
            return await self.forbid("profile.rename")
        name = str(data.get("name", "")).strip()[:80]
        if name:
            me.name = name
            await self._updated(me)

    async def on_moderate(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        action = str(data.get("action", ""))
        need = MODERATION.get(action)
        if need is None:
            return await self.error("bad_action", f"unknown moderation action {action!r}")
        if not me.can(need):
            return await self.forbid(need)
        extra = {k: v for k, v in data.items() if k not in ("type", "action", "target")}
        extra["by"] = me.name
        extra["by_rank"] = me.rank
        extra["override_rank"] = me.can("moderate.override_rank")
        if action in TARGETED:
            target = room.participants.get(str(data.get("target", "")))
            if target is None:
                return await self.error("unknown_peer", "no such participant")
            problem = can_act_on(me.rank, extra["override_rank"], target)
            if problem:
                return await self.error("forbidden", problem)
            if action == "set-role":
                role = await self.meet.roles.get(str(data.get("role", "")), room.config.tenant_id)
                if role is None:
                    return await self.error("unknown_role", "no such role")
                if role.rank >= me.rank and not extra["override_rank"]:
                    return await self.error("forbidden", "you can only assign roles below your own")
            await self.meet.hooks.emit("on_moderation", room, me, action, target, extra)
            if target.remote and room.bus is not None:  # the target's server applies it
                await room.bus.control(room.id, target.peer_id, action, extra)
            else:
                await moderate(self.meet, room, target, action, extra)
            return
        await self.meet.hooks.emit("on_moderation", room, me, action, None, extra)
        await room_action(self.meet, room, me, action, data, extra)

    # -- SFU -------------------------------------------------------------------
    def _sdp(self, data: Dict[str, Any]) -> Dict[str, str]:
        return {"sdp": str(data.get("sdp", "")), "type": str(data.get("sdpType", "offer"))}

    async def on_sfu_publish(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        if room.topology != "sfu" or not self.meet.sfu.available:
            return await self.error("not_sfu", "room is not using the SFU")
        if not room.can_publish(me):
            return await self.forbid("audio.publish / video.publish")
        answer = await self.meet.sfu.publish(room.id, me.peer_id, self._sdp(data))
        await self.send({"type": "sfu-publish-answer", "sdp": answer["sdp"],
                         "sdpType": answer["type"]})
        await room.broadcast({"type": "publisher", "peer_id": me.peer_id, "publishing": True},
                             exclude=me.peer_id)

    async def on_sfu_subscribe(self, data: Dict[str, Any]) -> None:
        room, me = self.room, self.me
        assert room and me
        publisher = str(data.get("publisher", ""))
        if room.topology != "sfu" or not self.meet.sfu.available:
            return await self.error("not_sfu", "room is not using the SFU")
        kinds = {k for k, perm in (("audio", "audio.subscribe"), ("video", "video.subscribe"))
                 if me.can(perm)}
        if not kinds:
            return await self.forbid("audio.subscribe / video.subscribe")
        try:
            answer = await self.meet.sfu.subscribe(room.id, me.peer_id, publisher,
                                                   self._sdp(data), kinds=kinds)
        except ValueError as exc:
            return await self.error("not_publishing", str(exc))
        await self.send({"type": "sfu-subscribe-answer", "publisher": publisher,
                         "sdp": answer["sdp"], "sdpType": answer["type"]})

    async def on_sfu_unsubscribe(self, data: Dict[str, Any]) -> None:
        assert self.room and self.me
        await self.meet.sfu.unsubscribe(self.room.id, self.me.peer_id,
                                        str(data.get("publisher", "")))


def can_act_on(actor_rank: int, override: bool, target: Participant) -> Optional[str]:
    if target.attributes.get("immune"):
        return f"{target.name} cannot be moderated"
    if not override and target.rank >= actor_rank:
        return f"{target.name} has the same or a higher rank than you"
    return None


async def updated(meet: "NodeMeet", room: Room, p: Participant) -> None:
    await meet.rooms.publish_state(room, p)
    await room.broadcast({"type": "peer-updated", "participant": p.to_public(room)})


async def _config_changed(meet: "NodeMeet", room: Room) -> None:
    await meet.rooms.update_config(room.config)
    await room.broadcast({"type": "room-updated", "room": room.snapshot()})


async def moderate(meet: "NodeMeet", room: Room, target: Optional[Participant], action: str,
                   data: Dict[str, Any]) -> None:
    """Apply a moderation action to someone connected to *this* server."""
    by = data.get("by", "host")
    if target is None:  # lobby decisions forwarded from another server
        waiting = room.lobby.get(str(data.get("peer_id", "")))
        if waiting is not None and action in ("admit", "deny", "block"):
            waiting.decide(action == "admit", blocked=action == "block")
        return
    if action == "mute":
        target.audio_muted, target.audio_locked = True, True
        await room.send_to(target.peer_id, {"type": "force-mute", "by": by})
    elif action == "ask-unmute":
        target.audio_locked = False
        await room.send_to(target.peer_id, {"type": "unmute-request", "by": by})
    elif action == "allow-unmute":
        target.audio_locked = False
        await room.send_to(target.peer_id, {"type": "unmute-allowed", "by": by})
    elif action == "video-off":
        target.video_muted, target.video_locked = True, True
        await room.send_to(target.peer_id, {"type": "force-video-off", "by": by})
    elif action == "allow-video":
        target.video_locked = False
        await room.send_to(target.peer_id, {"type": "video-allowed", "by": by})
    elif action == "stop-screen":
        target.screen_sharing = False
        await room.send_to(target.peer_id, {"type": "force-stop-screen", "by": by})
    elif action == "lower-hand":
        target.hand_raised = False
    elif action == "rename":
        name = str(data.get("name", "")).strip()[:80]
        if not name:
            return
        target.name = name
    elif action in ("kick", "ban"):
        if action == "ban":
            await ban_user(meet, room, target.user_id, target.name, by, data.get("reason"),
                           device=target.attributes.get("_device"), ip=target.attributes.get("_ip"),
                           scope=data.get("scope"))
        await room.send_to(target.peer_id, {"type": "banned" if action == "ban" else "kicked", "by": by})
        if target.close:
            await target.close()
        return
    elif action in ("allow", "disallow"):
        cfg = room.config
        if action == "allow" and not cfg.on_join_list(target.user_id):
            cfg.allowed_users.append(target.user_id)
        elif action == "disallow":
            cfg.allowed_users = [u for u in cfg.allowed_users if u.lower() != target.user_id.lower()]
        await _config_changed(meet, room)
        return
    elif action in ("promote", "demote"):
        target.promoted = action == "promote"
        if not target.promoted:
            await meet.sfu.unpublish(room.id, target.peer_id)
        await _send_permissions(room, target)
    elif action == "set-role":
        old = target.role
        eff = await meet.roles.resolve(str(data.get("role")), room.config.tenant_id,
                                       room_overrides=room.config.role_overrides,
                                       grant=target.attributes.get("_grant") or (),
                                       revoke=target.attributes.get("_revoke") or ())
        hidden = {k: v for k, v in target.attributes.items() if str(k).startswith("_")}
        target.role, target.permissions = eff.name, set(eff.permissions)
        target.attributes = {**eff.attributes, **hidden}
        if not room.can_publish(target):
            await meet.sfu.unpublish(room.id, target.peer_id)
        await _send_permissions(room, target)
        await meet.hooks.emit("on_role_changed", room, target, old)
    await updated(meet, room, target)


async def _send_permissions(room: Room, target: Participant) -> None:
    await room.send_to(target.peer_id, {"type": "permissions",
                                        "permissions": sorted(target.permissions),
                                        "can_publish": room.can_publish(target),
                                        "you": target.to_public(room)})


async def room_action(meet: "NodeMeet", room: Room, me: Participant, action: str,
                      data: Dict[str, Any], extra: Dict[str, Any]) -> None:
    cfg = room.config
    if action in ("mute-all", "lower-all-hands"):
        single = "mute" if action == "mute-all" else "lower-hand"
        for p in list(room.participants.values()):
            if p.peer_id == me.peer_id or can_act_on(me.rank, extra["override_rank"], p):
                continue
            if single == "lower-hand" and not p.hand_raised:
                continue
            if p.remote and room.bus is not None:
                await room.bus.control(room.id, p.peer_id, single, extra)
            else:
                await moderate(meet, room, p, single, extra)
    elif action == "spotlight":
        target = data.get("target") or None
        if target and target not in room.participants:
            return
        room.spotlight = target
        await room.broadcast({"type": "spotlight", "peer_id": target, "by": me.name})
    elif action in ("admit", "deny", "admit-all", "admit-always", "block"):
        ids = list(room.lobby) if action == "admit-all" else [str(data.get("target", ""))]
        admit = action in ("admit", "admit-all", "admit-always")
        for pid in ids:
            waiting = room.lobby.get(pid)
            person = waiting.waiting if waiting is not None else None
            if action == "admit-always" and person is not None and not cfg.on_join_list(person.user_id):
                cfg.allowed_users.append(person.user_id)
                await _config_changed(meet, room)
            if action == "block" and person is not None:
                await ban_user(meet, room, person.user_id, person.name, me.name, data.get("reason"),
                               device=person.attributes.get("_device"), ip=person.attributes.get("_ip"),
                               scope=data.get("scope"))
            if waiting is not None:
                waiting.decide(admit, blocked=action == "block")
                if admit:
                    await meet.hooks.emit("on_admitted", room, person, me)
            elif room.bus is not None:
                await room.bus.control(room.id, pid, "admit" if admit else
                                       ("block" if action == "block" else "deny"), extra)
    elif action == "lobby-message":
        cfg.lobby_message = str(data.get("text", ""))[:500]
        await _config_changed(meet, room)
        for waiting in list(room.lobby.values()):
            await waiting.ws.send_text(json.dumps({"type": "lobby-message", "text": cfg.lobby_message}))
    elif action == "lobby-mode":
        cfg.lobby_mode = "until_host" if data.get("mode") == "until_host" else "manual"
        await _config_changed(meet, room)
    elif action in ("lobby-on", "lobby-off"):
        cfg.lobby = action == "lobby-on"
        await _config_changed(meet, room)
    elif action in ("lock", "unlock"):
        cfg.locked = action == "lock"
        await _config_changed(meet, room)
    elif action in ("chat-on", "chat-off"):
        cfg.chat_enabled = action == "chat-on"
        await _config_changed(meet, room)
    elif action == "unban":
        uid = str(data.get("user_id", "")).lower()
        if cfg.is_banned(uid):
            cfg.banned_users = [u for u in cfg.banned_users if u.lower() != uid]
            info = next((v for k, v in cfg.banned_info.items() if k.lower() == uid), {}) or {}
            cfg.banned_devices = [d for d in cfg.banned_devices if d != info.get("device")]
            cfg.banned_ips = [i for i in cfg.banned_ips if i != info.get("ip")]
            cfg.banned_info = {k: v for k, v in cfg.banned_info.items() if k.lower() != uid}
            room.ban_attempts.pop(uid, None)
            await _config_changed(meet, room)
    elif action == "end":
        await end_meeting(meet, room, by=me.name)


async def ban_user(meet: "NodeMeet", room: Room, user_id: str, name: str, by: str,
                   reason: Any = None, *, device: Optional[str] = None, ip: Optional[str] = None,
                   scope: Any = None) -> None:
    """Block a user from this room: recorded with who/when, and removed from the join list
    (if they're unbanned later they go through the waiting room again).

    ``scope`` (default ``NodeMeet(ban_scope=...)``, i.e. user + device) can add "ip"."""
    cfg = room.config
    scope = set(scope or meet.ban_scope)
    if not cfg.is_banned(user_id):
        cfg.banned_users.append(user_id)
    if "device" in scope and device and device not in cfg.banned_devices:
        cfg.banned_devices.append(device)
    if "ip" in scope and ip and ip not in cfg.banned_ips:
        cfg.banned_ips.append(ip)
    cfg.banned_info[user_id] = {"name": name, "by": by, "at": time.time(),
                                "reason": str(reason)[:200] if reason else None,
                                "device": device if "device" in scope else None,
                                "ip": ip if "ip" in scope else None}
    cfg.allowed_users = [u for u in cfg.allowed_users if u.lower() != user_id.lower()]
    await _config_changed(meet, room)


async def notify_lobby(room: Room) -> None:
    """Tell everyone waiting where they are in line."""
    total = len(room.lobby)
    for i, waiting in enumerate(list(room.lobby.values()), 1):
        try:
            await waiting.ws.send_text(json.dumps({"type": "lobby-position", "position": i, "total": total}))
        except Exception:  # noqa: BLE001 - they may have just left
            pass


async def end_meeting(meet: "NodeMeet", room: Room, by: str = "host") -> None:
    await room.broadcast({"type": "room-closed", "reason": "ended", "by": by})
    for waiting in list(room.lobby.values()):
        waiting.decide(False)
    for p in list(room.local_participants()):
        if p.close:
            await p.close()


async def serve_session(meet: "NodeMeet", ws: WSTransport) -> None:
    """Run one signaling session over any WebSocket transport."""
    session = Session(meet, ws)
    meet._sessions.add(session)
    try:
        await session.run()
    finally:
        meet._sessions.discard(session)
        # Servers such as aiohttp cancel the handler when the client drops. Cleanup (peer-left,
        # topology switch, on_leave hooks, webhooks) must still finish, so it runs shielded.
        task = asyncio.ensure_future(session.cleanup())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise  # the task keeps running; the server just stops waiting for it
