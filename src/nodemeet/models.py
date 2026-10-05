"""Plain dataclasses shared by storage backends, the REST API and services.

Everything serialises to/from JSON-friendly dicts so you can persist it in
any database (see :class:`nodemeet.storage.Storage`).
"""
from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

ROOM_MODES = ("auto", "p2p", "sfu", "webinar")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime. Naive datetimes are assumed to be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return to_utc(datetime.fromisoformat(text))


def fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return to_utc(dt).isoformat().replace("+00:00", "Z") if dt else None


@dataclass
class RoomConfig:
    """Persistent room settings. Live state (who is connected) is in RoomManager."""

    id: str = field(default_factory=lambda: new_id("room_"))
    name: str = ""
    mode: str = "auto"  # auto | p2p | sfu | webinar
    max_participants: Optional[int] = None  # None = no cap
    locked: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    tenant_id: Optional[str] = None  # owner when multi-tenant API keys are used
    lobby: bool = True  # waiting room (on by default): people not on the join list wait to be admitted
    allowed_users: List[str] = field(default_factory=list)  # "join list": these user ids/emails skip the waiting room
    chat_enabled: bool = True
    banned_users: List[str] = field(default_factory=list)
    banned_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # user_id -> name/by/at/reason
    banned_devices: List[str] = field(default_factory=list)  # browser device ids of blocked people
    banned_ips: List[str] = field(default_factory=list)  # only filled when ban scope includes "ip"
    lobby_mode: str = "manual"  # manual | until_host (everyone waits only until a host is there)
    lobby_message: str = ""  # shown to people in the waiting room (live-editable)
    sso_required: Optional[str] = None  # SSO provider name people must sign in with
    role_overrides: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)  # role -> grant/revoke
    branding: Dict[str, Any] = field(default_factory=dict)  # per-room branding overrides
    default_role: Optional[str] = None  # role for tokens that don't name one

    def on_join_list(self, user_id: str) -> bool:
        uid = str(user_id).strip().lower()
        return any(uid == str(u).strip().lower() for u in self.allowed_users)

    def is_banned(self, user_id: str, device: Optional[str] = None, ip: Optional[str] = None) -> bool:
        uid = str(user_id).strip().lower()
        if any(uid == str(u).strip().lower() for u in self.banned_users):
            return True
        if device and device in self.banned_devices:
            return True
        return bool(ip) and ip in self.banned_ips

    def __post_init__(self) -> None:
        if self.mode not in ROOM_MODES:
            raise ValueError(f"mode must be one of {ROOM_MODES}")
        if self.lobby_mode not in ("manual", "until_host"):
            raise ValueError("lobby_mode must be 'manual' or 'until_host'")
        if self.role_overrides:
            from .permissions import validate_permissions
            for role, ov in self.role_overrides.items():
                if set(ov) - {"grant", "revoke"}:
                    raise ValueError("role_overrides entries take 'grant' and/or 'revoke'")
                validate_permissions(ov.get("grant") or [])
                validate_permissions(ov.get("revoke") or [])
        if self.branding:
            from .branding import validate
            validate(self.branding)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["created_at"] = fmt_dt(self.created_at)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RoomConfig":
        return cls(id=d["id"], name=d.get("name", ""), mode=d.get("mode", "auto"),
                   max_participants=d.get("max_participants"), locked=bool(d.get("locked")),
                   metadata=dict(d.get("metadata") or {}),
                   created_at=parse_dt(d.get("created_at")) or utcnow(),
                   tenant_id=d.get("tenant_id"), lobby=d.get("lobby", True) is not False,
                   allowed_users=list(d.get("allowed_users") or []),
                   banned_info=dict(d.get("banned_info") or {}),
                   banned_devices=list(d.get("banned_devices") or []),
                   banned_ips=list(d.get("banned_ips") or []),
                   lobby_mode=d.get("lobby_mode") or "manual", lobby_message=d.get("lobby_message") or "",
                   sso_required=d.get("sso_required"),
                   chat_enabled=d.get("chat_enabled", True) is not False,
                   banned_users=list(d.get("banned_users") or []),
                   role_overrides=dict(d.get("role_overrides") or {}),
                   branding=dict(d.get("branding") or {}), default_role=d.get("default_role"))


@dataclass
class ChatMessage:
    room: str
    peer_id: str
    user_id: str
    name: str
    text: str
    id: str = field(default_factory=lambda: new_id("msg_"))
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChatMessage":
        return cls(**{k: d[k] for k in ("room", "peer_id", "user_id", "name", "text", "id", "ts")})


class BookingStatus(str, Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@dataclass
class Booking:
    host_id: str
    start: datetime
    end: datetime
    attendee_name: str
    attendee_email: str
    id: str = field(default_factory=lambda: new_id("bk_"))
    title: str = "Meeting"
    notes: str = ""
    status: BookingStatus = BookingStatus.CONFIRMED
    room: str = field(default_factory=lambda: new_id("room_"))
    attendee_timezone: Optional[str] = None
    manage_token: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    sequence: int = 0  # bumped on every change; used by .ics updates
    reminders_sent: List[int] = field(default_factory=list)  # offsets in minutes
    cancel_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    tenant_id: Optional[str] = None
    attendee_phone: Optional[str] = None  # E.164, e.g. +919812345678 (SMS / WhatsApp)

    @property
    def payment(self) -> Dict[str, Any]:
        return dict(self.metadata.get("payment") or {})

    @property
    def awaiting_payment(self) -> bool:
        return self.payment.get("status") == "pending"

    def __post_init__(self) -> None:
        self.start = to_utc(self.start)
        self.end = to_utc(self.end)
        if self.end <= self.start:
            raise ValueError("booking end must be after start")
        self.status = BookingStatus(self.status)

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_active(self) -> bool:
        return self.status == BookingStatus.CONFIRMED

    def to_dict(self, *, include_secrets: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        for key in ("start", "end", "created_at", "updated_at"):
            d[key] = fmt_dt(getattr(self, key))
        if not include_secrets:
            d.pop("manage_token", None)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Booking":
        data = dict(d)
        for key in ("start", "end", "created_at", "updated_at"):
            if key in data:
                data[key] = parse_dt(data[key])
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})
