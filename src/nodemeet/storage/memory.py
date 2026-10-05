"""In-memory storage. Great for tests, demos and single-process dev servers."""
from __future__ import annotations

import copy
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..integrations.webhooks import WebhookEndpoint
from ..models import Booking, BookingStatus, ChatMessage, RoomConfig, to_utc
from ..permissions import RoleDefinition
from ..tenancy import ApiKey
from ..scheduling.availability import Availability
from .base import Storage, _matches


class MemoryStorage(Storage):
    def __init__(self, chat_history: int = 500) -> None:
        self.rooms: Dict[str, RoomConfig] = {}
        self.availability: Dict[str, Availability] = {}
        self.bookings: Dict[str, Booking] = {}
        self.chat: Dict[str, Deque[ChatMessage]] = defaultdict(lambda: deque(maxlen=chat_history))
        self.api_keys: Dict[str, ApiKey] = {}
        self.webhooks: Dict[str, WebhookEndpoint] = {}
        self.roles: Dict[tuple, RoleDefinition] = {}
        self.branding: Dict[str, Dict[str, Any]] = {}
        self.records: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def save_room(self, room: RoomConfig) -> None:
        self.rooms[room.id] = copy.deepcopy(room)

    async def get_room(self, room_id: str) -> Optional[RoomConfig]:
        r = self.rooms.get(room_id)
        return copy.deepcopy(r) if r else None

    async def delete_room(self, room_id: str) -> None:
        self.rooms.pop(room_id, None)
        self.chat.pop(room_id, None)

    async def list_rooms(self) -> List[RoomConfig]:
        return [copy.deepcopy(r) for r in self.rooms.values()]

    async def save_availability(self, availability: Availability) -> None:
        self.availability[availability.host_id] = copy.deepcopy(availability)

    async def get_availability(self, host_id: str) -> Optional[Availability]:
        a = self.availability.get(host_id)
        return copy.deepcopy(a) if a else None

    async def save_booking(self, booking: Booking) -> None:
        self.bookings[booking.id] = copy.deepcopy(booking)

    async def get_booking(self, booking_id: str) -> Optional[Booking]:
        b = self.bookings.get(booking_id)
        return copy.deepcopy(b) if b else None

    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        s = to_utc(start) if start else None
        e = to_utc(end) if end else None
        found = [copy.deepcopy(b) for b in self.bookings.values()
                 if _matches(b, host_id, s, e, status)]
        return sorted(found, key=lambda b: b.start)

    async def save_chat(self, message: ChatMessage) -> None:
        self.chat[message.room].append(message)

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        return list(self.chat.get(room_id, ()))[-limit:]

    async def save_api_key(self, key: ApiKey) -> None:
        self.api_keys[key.id] = copy.deepcopy(key)

    async def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        k = self.api_keys.get(key_id)
        return copy.deepcopy(k) if k else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        for k in self.api_keys.values():
            if k.key_hash == key_hash:
                return copy.deepcopy(k)
        return None

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List[ApiKey]:
        return [copy.deepcopy(k) for k in self.api_keys.values()
                if tenant_id is None or k.tenant_id == tenant_id]

    async def save_webhook(self, endpoint: WebhookEndpoint) -> None:
        self.webhooks[endpoint.id] = copy.deepcopy(endpoint)

    async def delete_webhook(self, webhook_id: str) -> None:
        self.webhooks.pop(webhook_id, None)

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List[WebhookEndpoint]:
        return [copy.deepcopy(w) for w in self.webhooks.values()
                if tenant_id is None or w.tenant_id == tenant_id]

    async def delete_chat(self, room_id: str, message_id: str) -> None:
        q = self.chat.get(room_id)
        if q is not None:
            for m in list(q):
                if m.id == message_id:
                    q.remove(m)

    async def save_role(self, role: RoleDefinition) -> None:
        self.roles[(role.tenant_id, role.name)] = copy.deepcopy(role)

    async def list_roles(self, tenant_id: Optional[str] = None) -> List[RoleDefinition]:
        return [copy.deepcopy(r) for (t, _), r in self.roles.items() if t == tenant_id]

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        self.roles.pop((tenant_id, name), None)

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        self.branding[key] = copy.deepcopy(branding)

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        b = self.branding.get(key)
        return copy.deepcopy(b) if b is not None else None

    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        self.records.setdefault(kind, {})[key] = copy.deepcopy(value)

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        v = self.records.get(kind, {}).get(key)
        return copy.deepcopy(v) if v is not None else None

    async def delete_record(self, kind: str, key: str) -> None:
        self.records.get(kind, {}).pop(key, None)

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        return sorted((k, copy.deepcopy(v)) for k, v in self.records.get(kind, {}).items()
                      if k.startswith(prefix))
