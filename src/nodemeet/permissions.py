"""Permissions and roles.

Every capability in a meeting is a named permission (see ``PERMISSIONS``).
A *role* is a named bundle of permissions plus attributes (rank, colour,
badge, media limits, and any custom key/values you like).

* The three built-in roles (host, participant, viewer) can be edited or
  deleted; ``reset`` restores the shipped definition.
* Add as many custom roles as you want (``teacher``, ``interpreter``,
  ``recorder``...), globally or per tenant.
* Per room you can grant/revoke permissions for a role (``RoomConfig.role_overrides``)
  and per user through the join token (``grant=[...]``, ``revoke=[...]``).

Resolution order: tenant role > global role > built-in, then room
overrides, then token grants/revokes, then live changes in the meeting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set

# -- permission catalogue ------------------------------------------------------
PERMISSIONS: Dict[str, Dict[str, str]] = {
    "access": {
        "room.join": "Join the meeting at all",
        "room.join_locked": "Join even when the room is locked",
        "room.join_full": "Join even when max_participants is reached",
        "room.bypass_lobby": "Skip the waiting room",
        "participants.list": "See other participants' names and details",
        "participants.see_hidden": "See participants whose role is hidden",
    },
    "media": {
        "audio.publish": "Send microphone audio",
        "video.publish": "Send camera video",
        "screen.publish": "Share the screen",
        "screen.audio": "Include tab/system audio when sharing the screen",
        "audio.subscribe": "Hear others",
        "video.subscribe": "See others' cameras",
        "screen.subscribe": "See others' screen shares",
        "audio.unmute_self": "Unmute yourself after a moderator muted you",
        "video.start_self": "Turn your camera back on after a moderator stopped it",
        "media.hd": "Send video above 720p",
    },
    "chat": {
        "chat.read": "Receive chat messages",
        "chat.history": "See earlier messages when joining",
        "chat.send": "Send messages to everyone",
        "chat.private": "Send private messages to one participant",
        "chat.links": "Send messages that contain links",
        "chat.delete_own": "Delete your own messages",
        "chat.delete_any": "Delete anyone's messages",
        "chat.pin": "Pin a message for everyone",
        "reactions.send": "Send emoji reactions",
    },
    "self": {
        "hand.raise": "Raise and lower your hand",
        "profile.rename": "Change your display name",
        "layout.change": "Switch your own layout (grid/speaker/sidebar)",
    },
    "collaboration": {
        "polls.create": "Create, close and delete polls",
        "polls.vote": "Vote in polls",
        "polls.results": "See live poll results before a poll closes",
        "qa.ask": "Ask questions in Q&A",
        "qa.upvote": "Upvote questions",
        "qa.answer": "Answer, highlight or dismiss questions",
        "whiteboard.view": "See the shared whiteboard",
        "whiteboard.draw": "Draw on the whiteboard",
        "whiteboard.clear": "Clear the whole whiteboard",
        "breakout.manage": "Create, open and close breakout rooms",
        "breakout.choose": "Pick your own breakout room",
    },
    "recording": {
        "recording.start": "Start and stop recording",
        "recording.download": "Download recordings of this room",
        "captions.enable": "Turn live captions on/off for everyone",
        "captions.view": "See live captions",
        "transcript.download": "Download the transcript",
    },
    "moderation": {
        "moderate.mute": "Mute someone's microphone",
        "moderate.mute_all": "Mute everyone at once",
        "moderate.ask_unmute": "Ask someone to unmute / allow them to",
        "moderate.video_off": "Turn off someone's camera",
        "moderate.stop_screen": "Stop someone's screen share",
        "moderate.lower_hands": "Lower someone's hand",
        "moderate.rename": "Rename other participants",
        "moderate.kick": "Remove someone from the meeting",
        "moderate.ban": "Remove and block someone from rejoining this room",
        "moderate.stage": "Invite to / remove from stage in webinars",
        "moderate.assign_roles": "Change a participant's role during the meeting",
        "moderate.spotlight": "Spotlight a participant for everyone",
        "moderate.lobby": "Admit or deny people in the waiting room",
        "moderate.lock": "Lock and unlock the room",
        "moderate.chat": "Turn chat on/off for everyone",
        "moderate.end": "End the meeting for everyone",
        "moderate.override_rank": "Moderate people of equal or higher rank",
    },
}
ALL_PERMISSIONS: FrozenSet[str] = frozenset(p for group in PERMISSIONS.values() for p in group)

# Attributes with built-in meaning. Anything else you add is passed through to
# clients (participant.attributes) and your hooks untouched.
KNOWN_ATTRIBUTES: Dict[str, str] = {
    "label": "Display name of the role",
    "description": "What the role is for",
    "color": "Badge colour (CSS colour)",
    "badge": "Short badge text or emoji shown on tiles",
    "rank": "Integer; moderators can only act on lower ranks",
    "hidden": "Invisible to people without participants.see_hidden",
    "stage": "Publishes by default in webinar rooms",
    "auto_mute": "Join with the microphone muted",
    "auto_video_off": "Join with the camera off",
    "max_video_height": "Cap sent video height (e.g. 360, 720, 1080)",
    "max_fps": "Cap sent frame rate",
    "max_bitrate_kbps": "Cap sent video bitrate",
    "chat_per_minute": "Chat rate limit (messages per minute)",
    "immune": "Cannot be moderated by anyone",
}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _expand(perms: Iterable[str]) -> Set[str]:
    """Support wildcards: ``*`` and ``chat.*`` / ``moderate.*``."""
    from .easy import did_you_mean, friendly_permissions
    out: Set[str] = set()
    for p in friendly_permissions(list(perms)):
        p = str(p).strip()
        if p == "*":
            out |= ALL_PERMISSIONS
        elif p.endswith(".*"):
            prefix = p[:-1]
            matched = {x for x in ALL_PERMISSIONS if x.startswith(prefix)}
            if not matched:
                raise ValueError(f"no permissions match {p!r}")
            out |= matched
        elif p in ALL_PERMISSIONS:
            out.add(p)
        else:
            raise ValueError(f"unknown permission {p!r}." + did_you_mean(p, sorted(ALL_PERMISSIONS)))
    return out


def validate_permissions(perms: Iterable[str]) -> Set[str]:
    return _expand(perms)


@dataclass
class RoleDefinition:
    name: str
    permissions: Set[str] = field(default_factory=set)
    attributes: Dict[str, Any] = field(default_factory=dict)
    tenant_id: Optional[str] = None  # None = global
    builtin: bool = False
    deleted: bool = False  # tombstone: hides a built-in/global role

    def __post_init__(self) -> None:
        self.name = str(self.name).strip().lower()
        if not _NAME_RE.match(self.name):
            raise ValueError("role names are 1-64 chars of a-z, 0-9, _ . -")
        self.permissions = _expand(self.permissions)
        rank = self.attributes.get("rank", 0)
        if not isinstance(rank, int):
            raise ValueError("attribute 'rank' must be an integer")

    @property
    def rank(self) -> int:
        return int(self.attributes.get("rank", 0))

    @property
    def label(self) -> str:
        return str(self.attributes.get("label") or self.name.replace("_", " ").title())

    def can(self, perm: str) -> bool:
        return perm in self.permissions

    def public(self) -> Dict[str, Any]:
        return {"name": self.name, "label": self.label, "rank": self.rank,
                "color": self.attributes.get("color"), "badge": self.attributes.get("badge"),
                "description": self.attributes.get("description", ""),
                "hidden": bool(self.attributes.get("hidden"))}

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "permissions": sorted(self.permissions),
                "attributes": dict(self.attributes), "tenant_id": self.tenant_id,
                "builtin": self.builtin, "deleted": self.deleted}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RoleDefinition":
        return cls(name=d["name"], permissions=set(d.get("permissions") or []),
                   attributes=dict(d.get("attributes") or {}), tenant_id=d.get("tenant_id"),
                   builtin=bool(d.get("builtin")), deleted=bool(d.get("deleted")))


_PARTICIPANT = {
    "room.join", "participants.list", "audio.publish", "video.publish", "screen.publish",
    "screen.audio", "audio.subscribe", "video.subscribe", "screen.subscribe",
    "video.start_self", "media.hd", "chat.read", "chat.history", "chat.send", "chat.private",
    "chat.links", "chat.delete_own", "reactions.send", "hand.raise", "profile.rename",
    "layout.change", "polls.vote", "qa.ask", "qa.upvote", "whiteboard.view", "whiteboard.draw",
    "captions.view",
}
_VIEWER = {
    "room.join", "participants.list", "audio.subscribe", "video.subscribe", "screen.subscribe",
    "chat.read", "chat.history", "chat.send", "reactions.send", "hand.raise", "layout.change",
    "polls.vote", "qa.ask", "qa.upvote", "whiteboard.view", "captions.view",
}


def builtin_roles() -> Dict[str, RoleDefinition]:
    return {
        "host": RoleDefinition("host", {"*"}, {"label": "Host", "rank": 100, "color": "#f59e0b",
                                                "badge": "Host", "stage": True}, builtin=True),
        "participant": RoleDefinition("participant", set(_PARTICIPANT),
                                      {"label": "Participant", "rank": 10}, builtin=True),
        "viewer": RoleDefinition("viewer", set(_VIEWER), {"label": "Viewer", "rank": 0},
                                 builtin=True),
    }


def apply_overrides(perms: Set[str], override: Optional[Mapping[str, Any]]) -> Set[str]:
    """``{"grant": [...], "revoke": [...]}`` -> new permission set."""
    if not override:
        return set(perms)
    out = set(perms) | _expand(override.get("grant") or [])
    return out - _expand(override.get("revoke") or [])


class RoleRegistry:
    """Looks up roles with tenant > global > built-in precedence.

    Backed by storage (``save_role`` / ``list_roles``) when available, with a
    small cache so joins don't hit the database every time.
    """

    def __init__(self, storage: Any = None, *, cache_seconds: float = 10.0,
                 default_role: str = "participant") -> None:
        self.storage = storage
        self.cache_seconds = cache_seconds
        self.default_role = default_role
        self._cache: Dict[Optional[str], Any] = {}
        self._local: Dict[tuple, RoleDefinition] = {}  # used when storage lacks role support

    async def _load(self, tenant_id: Optional[str]) -> List[RoleDefinition]:
        import time
        hit = self._cache.get(tenant_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            if self.storage is None:
                raise NotImplementedError
            roles = await self.storage.list_roles(tenant_id)
        except NotImplementedError:
            roles = [r for (t, _), r in self._local.items() if t == tenant_id]
        self._cache[tenant_id] = (time.monotonic() + self.cache_seconds, roles)
        return roles

    def invalidate(self) -> None:
        self._cache.clear()

    async def all(self, tenant_id: Optional[str] = None, *, include_deleted: bool = False
                  ) -> Dict[str, RoleDefinition]:
        merged = builtin_roles()
        for layer in ([None] if tenant_id is None else [None, tenant_id]):
            for role in await self._load(layer):
                if layer is not None and role.tenant_id != layer:
                    continue
                if layer is None and role.tenant_id is not None:
                    continue
                merged[role.name] = role
        if not include_deleted:
            merged = {k: v for k, v in merged.items() if not v.deleted}
        return merged

    async def get(self, name: str, tenant_id: Optional[str] = None) -> Optional[RoleDefinition]:
        return (await self.all(tenant_id)).get(str(name).lower())

    async def save(self, role: RoleDefinition) -> RoleDefinition:
        role.builtin = role.name in builtin_roles() and role.tenant_id is None and role.builtin
        try:
            if self.storage is None:
                raise NotImplementedError
            await self.storage.save_role(role)
        except NotImplementedError:
            self._local[(role.tenant_id, role.name)] = role
        self.invalidate()
        return role

    async def create(self, name: str, permissions: Iterable[str] = (), *,
                     attributes: Optional[Dict[str, Any]] = None, tenant_id: Optional[str] = None,
                     based_on: Optional[str] = None) -> RoleDefinition:
        perms = set(permissions)
        attrs = dict(attributes or {})
        if based_on:
            base = await self.get(based_on, tenant_id)
            if base is None:
                raise ValueError(f"unknown base role {based_on!r}")
            perms |= base.permissions
            inherited = {k: v for k, v in base.attributes.items() if k not in ("label", "description")}
            attrs = {**inherited, **attrs}
        return await self.save(RoleDefinition(name, perms, attrs, tenant_id=tenant_id))

    async def update(self, name: str, *, tenant_id: Optional[str] = None,
                     permissions: Optional[Iterable[str]] = None, grant: Iterable[str] = (),
                     revoke: Iterable[str] = (), attributes: Optional[Dict[str, Any]] = None,
                     replace_attributes: bool = False) -> RoleDefinition:
        current = await self.get(name, tenant_id)
        if current is None:
            raise ValueError(f"unknown role {name!r}")
        perms = set(permissions) if permissions is not None else set(current.permissions)
        perms = apply_overrides(_expand(perms), {"grant": list(grant), "revoke": list(revoke)})
        attrs = dict(attributes or {}) if replace_attributes else {**current.attributes, **(attributes or {})}
        attrs = {k: v for k, v in attrs.items() if v is not None}  # None removes an attribute
        return await self.save(RoleDefinition(current.name, perms, attrs, tenant_id=tenant_id,
                                              builtin=current.builtin))

    async def delete(self, name: str, tenant_id: Optional[str] = None) -> None:
        current = await self.get(name, tenant_id)
        if current is None:
            raise ValueError(f"unknown role {name!r}")
        await self.save(RoleDefinition(current.name, set(), {}, tenant_id=tenant_id, deleted=True))

    async def reset(self, name: str, tenant_id: Optional[str] = None) -> None:
        """Remove a stored override/tombstone (built-ins come back as shipped)."""
        try:
            if self.storage is None:
                raise NotImplementedError
            await self.storage.delete_role(name, tenant_id)
        except NotImplementedError:
            self._local.pop((tenant_id, name), None)
        self.invalidate()

    async def resolve(self, name: str, tenant_id: Optional[str] = None, *,
                      room_overrides: Optional[Mapping[str, Any]] = None,
                      grant: Iterable[str] = (), revoke: Iterable[str] = ()) -> RoleDefinition:
        """Effective role for one participant (raises ValueError if it doesn't exist)."""
        role = await self.get(name, tenant_id)
        if role is None:
            raise ValueError(f"role {name!r} does not exist")
        perms = apply_overrides(role.permissions, (room_overrides or {}).get(role.name))
        perms = apply_overrides(perms, {"grant": list(grant), "revoke": list(revoke)})
        return RoleDefinition(role.name, perms, dict(role.attributes), tenant_id=role.tenant_id,
                              builtin=role.builtin)
