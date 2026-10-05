"""Keeps live rooms in sync across several nodemeet servers via a Broker.

Each server holds the participants connected to it (local) plus mirrors of
participants on other servers (remote). Joins/leaves/state changes, room
broadcasts, direct messages (WebRTC signaling) and moderation are relayed
over the broker, so a load balancer can send people in the same room to
different servers.

Media note: peer-to-peer rooms work across servers out of the box. The SFU
relays media *inside one server*, so for SFU/webinar rooms route each room to
one server (e.g. nginx ``hash $arg_room consistent;`` - the JS client adds
``?room=`` to the WebSocket URL for exactly this).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from .broker import Broker
from .rooms import Participant

if TYPE_CHECKING:
    from .rooms import Room, RoomManager

log = logging.getLogger("nodemeet.cluster")
NODE_TTL = 15.0
HEARTBEAT = 5.0
ControlFn = Callable[["Room", Optional[Participant], str, Dict[str, Any]], Awaitable[None]]


class ClusterBus:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.manager: Optional["RoomManager"] = None
        self.on_control: Optional[ControlFn] = None
        self._hb: Optional["asyncio.Task[None]"] = None

    @property
    def node_id(self) -> str:
        return self.broker.node_id

    @staticmethod
    def _chan(room_id: str) -> str:
        return f"room:{room_id}"

    @staticmethod
    def _presence(room_id: str) -> str:
        return f"presence:{room_id}"

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.broker.start()
        await self._beat()
        self._hb = asyncio.ensure_future(self._heartbeat())

    async def close(self) -> None:
        if self._hb:
            self._hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hb
        if self.manager is not None:  # remove our participants from shared presence
            for room in list(self.manager.live.values()):
                for p in room.local_participants():
                    await self.broker.hdel(self._presence(room.id), p.peer_id)
                    await self.broker.publish(self._chan(room.id), {
                        "kind": "leave", "origin": self.node_id, "peer_id": p.peer_id})
        await self.broker.delete(f"node:{self.node_id}")
        await self.broker.close()

    async def _beat(self) -> None:
        await self.broker.set(f"node:{self.node_id}", "1", ttl=NODE_TTL)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT)
            try:
                await self._beat()
            except Exception:  # noqa: BLE001
                log.exception("cluster heartbeat failed")

    # -- rooms -------------------------------------------------------------
    async def attach(self, room: "Room") -> None:
        room.bus = self
        await self.broker.subscribe(self._chan(room.id), lambda msg, r=room: self._on_message(r, msg))
        for peer_id, raw in (await self.broker.hgetall(self._presence(room.id))).items():
            state = json.loads(raw)
            node = state.get("node_id")
            if node == self.node_id or peer_id in room.participants:
                continue
            if node and not await self.broker.get(f"node:{node}"):  # crashed server
                await self.broker.hdel(self._presence(room.id), peer_id)
                continue
            room.participants[peer_id] = Participant.from_state(state)
        topo = await self.broker.get(f"topology:{room.id}")
        if topo:
            room.topology = topo

    async def detach(self, room: "Room") -> None:
        await self.broker.unsubscribe(self._chan(room.id))

    async def announce_join(self, room: "Room", p: Participant) -> None:
        await self.broker.hset(self._presence(room.id), p.peer_id, json.dumps(p.to_state()))
        await self.broker.publish(self._chan(room.id), {"kind": "join", "origin": self.node_id,
                                                        "participant": p.to_state()})

    async def announce_update(self, room: "Room", p: Participant) -> None:
        await self.broker.hset(self._presence(room.id), p.peer_id, json.dumps(p.to_state()))
        await self.broker.publish(self._chan(room.id), {"kind": "update", "origin": self.node_id,
                                                        "participant": p.to_state()})

    async def announce_leave(self, room: "Room", peer_id: str) -> None:
        await self.broker.hdel(self._presence(room.id), peer_id)
        await self.broker.publish(self._chan(room.id), {"kind": "leave", "origin": self.node_id,
                                                        "peer_id": peer_id})

    async def set_topology(self, room: "Room") -> None:
        await self.broker.set(f"topology:{room.id}", room.topology, ttl=86400)

    async def broadcast(self, room_id: str, message: Dict[str, Any], *,
                        exclude: Optional[str] = None) -> None:
        await self.broker.publish(self._chan(room_id), {"kind": "broadcast", "origin": self.node_id,
                                                        "exclude": exclude, "message": message})

    async def direct(self, room_id: str, peer_id: str, message: Dict[str, Any]) -> None:
        await self.broker.publish(self._chan(room_id), {"kind": "direct", "origin": self.node_id,
                                                        "to": peer_id, "message": message})

    async def control(self, room_id: str, peer_id: str, action: str, data: Dict[str, Any]) -> None:
        await self.broker.publish(self._chan(room_id), {"kind": "control", "origin": self.node_id,
                                                        "to": peer_id, "action": action, "data": data})

    # -- incoming ------------------------------------------------------------
    async def _on_message(self, room: "Room", msg: Dict[str, Any]) -> None:
        if msg.get("origin") == self.node_id:
            return
        kind = msg.get("kind")
        if kind == "join":
            p = Participant.from_state(msg["participant"])
            room.participants.setdefault(p.peer_id, p)
            self._recompute(room)
        elif kind == "update":
            st = msg["participant"]
            p = room.participants.get(st["peer_id"])
            if p is None:
                room.participants[st["peer_id"]] = Participant.from_state(st)
            elif p.remote:
                p.apply_state(st)
        elif kind == "leave":
            p = room.participants.get(msg["peer_id"])
            if p is not None and p.remote:
                del room.participants[msg["peer_id"]]
                self._recompute(room)
        elif kind == "broadcast":
            message = msg.get("message") or {}
            mtype = message.get("type")
            if mtype == "topology":
                room.topology = message.get("topology", room.topology)
            elif mtype == "chat" and message.get("message"):
                from .models import ChatMessage
                room.chat.append(ChatMessage.from_dict(message["message"]))
            elif mtype == "chat-deleted":
                for m in list(room.chat):
                    if m.id == message.get("id"):
                        room.chat.remove(m)
            elif mtype == "chat-pinned":
                room.pinned = message.get("message")
            elif mtype == "spotlight":
                room.spotlight = message.get("peer_id")
            elif isinstance(mtype, str) and (mtype.startswith("_") or mtype in (
                    "board", "recording", "captions-state")):
                from .features import apply_remote
                apply_remote(room, message)
            elif mtype == "room-updated":
                snap = message.get("room") or {}
                fresh = None
                if self.manager is not None:
                    fresh = await self.manager.storage.get_room(room.id)
                if fresh is not None:
                    room.config = fresh  # bans and the join list apply on every server
                else:
                    for key in ("locked", "lobby", "chat_enabled"):
                        if key in snap:
                            setattr(room.config, key, snap[key])
            await room.broadcast(message, exclude=msg.get("exclude"), local_only=True)
            if mtype == "room-closed":
                for waiting in list(room.lobby.values()):
                    waiting.decide(False)
                for p in room.local_participants():
                    if p.close:
                        await p.close()
        elif kind == "direct":
            target = room.participants.get(msg.get("to", ""))
            if target is not None and target.is_local:
                await room.send_to(target.peer_id, msg.get("message") or {})
        elif kind == "control":
            target = room.participants.get(msg.get("to", ""))
            if target is None and msg.get("to") in room.lobby and self.on_control is not None:
                await self.on_control(room, None, str(msg.get("action")),
                                      {**(msg.get("data") or {}), "peer_id": msg.get("to")})
                return
            if target is None or not target.is_local:
                return
            if msg.get("action") == "close" and target.close:
                await target.close()
            elif self.on_control is not None:
                await self.on_control(room, target, str(msg.get("action")), msg.get("data") or {})

    def _recompute(self, room: "Room") -> None:
        # Keep the local view of the topology in step; the node that caused the
        # change broadcasts the authoritative "topology" message.
        if self.manager is not None:
            room.recompute_topology(p2p_max=self.manager.p2p_max,
                                    sfu_available=self.manager.sfu_available)
