"""Live room state: who is connected, what they are doing, and chat."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

from .exceptions import RoomFull, RoomNotFound
from .hooks import Hooks
from .models import ChatMessage, RoomConfig
from .storage.base import Storage
from .tokens import Role
from .topology import P2P, select_topology

log = logging.getLogger("nodemeet.rooms")
SendFn = Callable[[Dict[str, Any]], Awaitable[None]]


MEDIA_PERMS = {"audio": "audio.publish", "video": "video.publish", "screen": "screen.publish"}


@dataclass(eq=False)
class Participant:
    user_id: str
    name: str
    role: str
    peer_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    permissions: Set[str] = field(default_factory=set)
    attributes: Dict[str, Any] = field(default_factory=dict)  # from the role (+ custom)
    audio_muted: bool = False
    video_muted: bool = False
    screen_sharing: bool = False
    hand_raised: bool = False
    promoted: bool = False  # webinar: invited to stage
    audio_locked: bool = False  # muted by a moderator; needs audio.unmute_self to unmute
    video_locked: bool = False
    joined_at: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)
    node_id: Optional[str] = None  # server the participant is connected to
    remote: bool = False  # True = mirrored from another server in the cluster
    send: Optional[SendFn] = field(default=None, repr=False)
    close: Optional[Callable[[], Awaitable[Any]]] = field(default=None, repr=False)
    chat_times: List[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.role = Role.parse(self.role)
        if not self.permissions and not self.remote:
            from .permissions import builtin_roles
            r = builtin_roles().get(self.role)
            if r is not None:
                self.permissions = set(r.permissions)
                self.attributes = {**r.attributes, **self.attributes}

    @property
    def is_local(self) -> bool:
        return not self.remote

    @property
    def rank(self) -> int:
        try:
            return int(self.attributes.get("rank", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def hidden(self) -> bool:
        return bool(self.attributes.get("hidden"))

    def can(self, perm: str) -> bool:
        return perm in self.permissions

    def to_state(self) -> Dict[str, Any]:
        """Full state for cluster sync (includes permissions)."""
        return {"peer_id": self.peer_id, "user_id": self.user_id, "name": self.name,
                "role": self.role, "permissions": sorted(self.permissions),
                "attributes": self.attributes, "audio_muted": self.audio_muted,
                "video_muted": self.video_muted, "screen_sharing": self.screen_sharing,
                "hand_raised": self.hand_raised, "promoted": self.promoted,
                "audio_locked": self.audio_locked, "video_locked": self.video_locked,
                "joined_at": self.joined_at, "node_id": self.node_id}

    @classmethod
    def from_state(cls, d: Dict[str, Any]) -> "Participant":
        return cls(user_id=d["user_id"], name=d["name"], role=d["role"], peer_id=d["peer_id"],
                   permissions=set(d.get("permissions") or []),
                   attributes=dict(d.get("attributes") or {}),
                   audio_muted=bool(d.get("audio_muted")), video_muted=bool(d.get("video_muted")),
                   screen_sharing=bool(d.get("screen_sharing")),
                   hand_raised=bool(d.get("hand_raised")), promoted=bool(d.get("promoted")),
                   audio_locked=bool(d.get("audio_locked")), video_locked=bool(d.get("video_locked")),
                   joined_at=float(d.get("joined_at") or time.time()), node_id=d.get("node_id"),
                   remote=True)

    def apply_state(self, d: Dict[str, Any]) -> None:
        for key in ("name", "role", "audio_muted", "video_muted", "screen_sharing", "hand_raised",
                    "promoted", "audio_locked", "video_locked"):
            if key in d:
                setattr(self, key, d[key])
        if "permissions" in d:
            self.permissions = set(d["permissions"])
        if "attributes" in d:
            self.attributes = dict(d["attributes"])

    def public_attributes(self) -> Dict[str, Any]:
        return {k: v for k, v in self.attributes.items() if not str(k).startswith("_")}

    def to_public(self, room: Optional["Room"] = None) -> Dict[str, Any]:
        pub = {k: (room.can_publish(self, k) if room else self.can(v)) for k, v in MEDIA_PERMS.items()}
        return {
            "peer_id": self.peer_id, "user_id": self.user_id, "name": self.name,
            "role": self.role, "role_label": self.attributes.get("label") or self.role.title(),
            "badge": self.attributes.get("badge"), "color": self.attributes.get("color"),
            "rank": self.rank, "hidden": self.hidden, "attributes": self.public_attributes(),
            "audio_muted": self.audio_muted, "video_muted": self.video_muted,
            "screen_sharing": self.screen_sharing, "hand_raised": self.hand_raised,
            "audio_locked": self.audio_locked, "video_locked": self.video_locked,
            "joined_at": self.joined_at, "can_publish": any(pub.values()), "publish": pub,
            "receive": {"audio": self.can("audio.subscribe"), "video": self.can("video.subscribe"),
                        "screen": self.can("screen.subscribe")},
            "promoted": self.promoted,
        }


class Room:
    def __init__(self, config: RoomConfig, *, chat_history: int = 200) -> None:
        self.config = config
        self.participants: Dict[str, Participant] = {}
        self.topology: str = P2P
        self.chat: Deque[ChatMessage] = deque(maxlen=chat_history)
        self.created_at = time.time()
        self.lock = asyncio.Lock()
        self.bus: Any = None  # ClusterBus when running several servers
        self.lobby: Dict[str, Any] = {}  # peer_id -> waiting Session
        self.spotlight: Optional[str] = None
        self.pinned: Optional[Dict[str, Any]] = None
        self.ban_attempts: Dict[str, float] = {}  # user_id -> last notification time
        # collaboration state (mirrored on every server in a cluster)
        self.polls: Dict[str, Dict[str, Any]] = {}
        self.questions: Dict[str, Dict[str, Any]] = {}
        self.board: List[Dict[str, Any]] = []
        self.breakouts: Dict[str, Any] = {}
        self.recording: Optional[Dict[str, Any]] = None
        self.captions: Dict[str, Any] = {"enabled": False}

    def local_participants(self) -> List[Participant]:
        return [p for p in self.participants.values() if p.is_local]

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def mode(self) -> str:
        return self.config.mode

    def __len__(self) -> int:
        return len(self.participants)

    def can_publish(self, p: Participant, kind: Optional[str] = None) -> bool:
        kinds = [kind] if kind else list(MEDIA_PERMS)
        if not any(p.can(MEDIA_PERMS[k]) for k in kinds):
            return False
        if self.mode == "webinar":
            return bool(p.attributes.get("stage")) or p.promoted
        return True

    def recompute_topology(self, *, p2p_max: int, sfu_available: bool) -> bool:
        """Update ``self.topology``; returns True if it changed."""
        new = select_topology(self.mode, len(self.participants), self.topology,
                              p2p_max=p2p_max, sfu_available=sfu_available)
        changed = new != self.topology
        self.topology = new
        return changed

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.config.name, "mode": self.mode,
                "topology": self.topology, "locked": self.config.locked,
                "lobby": self.config.lobby, "chat_enabled": self.config.chat_enabled,
                "spotlight": self.spotlight, "pinned": self.pinned,
                "join_list": list(self.config.allowed_users),  # moderators only (filtered)
                "lobby_mode": self.config.lobby_mode, "lobby_message": self.config.lobby_message,
                "recording": self.recording, "captions": self.captions,
                "banned": [{"user_id": u, **self.config.banned_info.get(u, {})}
                           for u in self.config.banned_users],  # moderators only (filtered)
                "participants": [p.to_public(self) for p in self.participants.values()]}

    async def send_to(self, peer_id: str, message: Dict[str, Any]) -> bool:
        p = self.participants.get(peer_id)
        if p is None:
            return False
        if p.remote:  # connected to another server
            if self.bus is None:
                return False
            await self.bus.direct(self.id, peer_id, message)
            return True
        if p.send is None:
            return False
        try:
            await p.send(message)
            return True
        except Exception:  # noqa: BLE001 - a dead socket must not break the room
            log.debug("send to %s failed", peer_id, exc_info=True)
            return False

    async def broadcast(self, message: Dict[str, Any], *, exclude: Optional[str] = None,
                        local_only: bool = False) -> None:
        targets = [p.peer_id for p in self.local_participants() if p.peer_id != exclude]
        await asyncio.gather(*(self.send_to(pid, message) for pid in targets))
        if self.bus is not None and not local_only:
            await self.bus.broadcast(self.id, message, exclude=exclude)


class RoomManager:
    """Keeps live :class:`Room` objects and persists :class:`RoomConfig` via storage."""

    def __init__(self, storage: Storage, hooks: Hooks, *, p2p_max: int = 4,
                 sfu_available: bool = False, chat_history: int = 200,
                 auto_create: bool = True, bus: Any = None, waiting_room: bool = True) -> None:
        self.waiting_room = waiting_room
        self.storage = storage
        self.hooks = hooks
        self.p2p_max = p2p_max
        self.sfu_available = sfu_available
        self.chat_history = chat_history
        self.auto_create = auto_create
        self.live: Dict[str, Room] = {}
        self.bus = bus  # nodemeet.cluster.ClusterBus or None
        self._lock: Optional[asyncio.Lock] = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:  # created lazily inside the running loop
            self._lock = asyncio.Lock()
        return self._lock

    async def create(self, config: Optional[RoomConfig] = None, **kwargs: Any) -> RoomConfig:
        config = config or RoomConfig(**kwargs)
        await self.storage.save_room(config)
        await self.hooks.emit("on_room_created", config)
        return config

    async def open(self, room_id: str, *, tenant_id: Optional[str] = None) -> Room:
        async with self.lock:
            room = self.live.get(room_id)
            if room is not None:
                return room
            config = await self.storage.get_room(room_id)
            if config is None:
                if not self.auto_create:
                    raise RoomNotFound(room_id)
                config = await self.create(RoomConfig(id=room_id, tenant_id=tenant_id,
                                                      lobby=self.waiting_room))
            room = Room(config, chat_history=self.chat_history)
            for msg in await self.storage.list_chat(room_id, self.chat_history):
                room.chat.append(msg)
            if self.bus is not None:
                await self.bus.attach(room)
            self.live[room_id] = room
            return room

    def get(self, room_id: str) -> Optional[Room]:
        return self.live.get(room_id)

    def check_capacity(self, room: Room) -> None:
        cap = room.config.max_participants
        if cap is not None and len(room.participants) >= cap:
            raise RoomFull(f"room {room.id} is full ({cap})")

    async def add(self, room: Room, participant: Participant) -> bool:
        """Add a participant; returns True if the topology changed."""
        room.participants[participant.peer_id] = participant
        if self.bus is not None:
            participant.node_id = self.bus.node_id
            await self.bus.announce_join(room, participant)
        return room.recompute_topology(p2p_max=self.p2p_max, sfu_available=self.sfu_available)

    async def publish_state(self, room: Room, participant: Participant) -> None:
        if self.bus is not None and participant.is_local:
            await self.bus.announce_update(room, participant)

    async def remove(self, room: Room, peer_id: str) -> Optional[Participant]:
        p = room.participants.pop(peer_id, None)
        if self.bus is not None and p is not None and p.is_local:
            await self.bus.announce_leave(room, peer_id)
        if not room.local_participants():
            async with self.lock:
                if not room.local_participants() and self.live.get(room.id) is room:
                    del self.live[room.id]
                    if self.bus is not None:
                        await self.bus.detach(room)
                    if not room.participants:  # empty everywhere
                        await self.hooks.emit("on_room_closed", room)
        return p

    async def update_config(self, config: RoomConfig) -> None:
        await self.storage.save_room(config)
        room = self.live.get(config.id)
        if room is not None:
            room.config = config

    async def delete(self, room_id: str) -> None:
        room = self.live.pop(room_id, None)
        if room is not None:
            await room.broadcast({"type": "room-closed", "reason": "deleted"})
            for p in list(room.participants.values()):
                if p.close:
                    await p.close()
                elif self.bus is not None:
                    await self.bus.control(room.id, p.peer_id, "close", {})
        await self.storage.delete_room(room_id)

    def stats(self) -> List[Dict[str, Any]]:
        return [{"id": r.id, "participants": len(r), "topology": r.topology}
                for r in self.live.values()]
