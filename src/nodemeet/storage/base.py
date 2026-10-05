"""Storage interface. Implement this to plug nodemeet into your own database.

Only a handful of async methods are needed. See ``examples/custom_storage.py``
for a Postgres/SQLAlchemy-style sketch, or subclass :class:`MemoryStorage`
and override only what you need.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, DefaultDict, Dict, List, Optional, Tuple

from ..models import Booking, BookingStatus, ChatMessage, RoomConfig
from ..scheduling.availability import Availability

if TYPE_CHECKING:
    from ..integrations.webhooks import WebhookEndpoint
    from ..permissions import RoleDefinition
    from ..tenancy import ApiKey


class Storage(ABC):
    async def setup(self) -> None:
        """Create tables / open connections. Called on server startup."""

    async def close(self) -> None:
        """Release resources. Called on server shutdown."""

    # -- rooms ---------------------------------------------------------
    @abstractmethod
    async def save_room(self, room: RoomConfig) -> None: ...

    @abstractmethod
    async def get_room(self, room_id: str) -> Optional[RoomConfig]: ...

    @abstractmethod
    async def delete_room(self, room_id: str) -> None: ...

    @abstractmethod
    async def list_rooms(self) -> List[RoomConfig]: ...

    # -- availability --------------------------------------------------
    @abstractmethod
    async def save_availability(self, availability: Availability) -> None: ...

    @abstractmethod
    async def get_availability(self, host_id: str) -> Optional[Availability]: ...

    # -- bookings ------------------------------------------------------
    @abstractmethod
    async def save_booking(self, booking: Booking) -> None:
        """Insert or update (upsert) a booking."""

    @abstractmethod
    async def get_booking(self, booking_id: str) -> Optional[Booking]: ...

    @abstractmethod
    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        """Bookings overlapping ``[start, end)``, ordered by start time."""

    # -- chat (optional; default keeps nothing) ------------------------
    async def save_chat(self, message: ChatMessage) -> None:
        return None

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        return []

    # -- concurrency-safe booking & reminders --------------------------
    # The defaults below are correct for ONE process. Database backends
    # override them with transactions / unique constraints so they stay
    # correct with many workers and many servers.
    _host_locks: DefaultDict[str, asyncio.Lock]

    def _lock_for(self, host_id: str) -> asyncio.Lock:
        if not hasattr(self, "_host_locks"):
            self._host_locks = defaultdict(asyncio.Lock)
        return self._host_locks[host_id]

    async def insert_booking_if_free(self, booking: Booking, *, buffer_before: int = 0,
                                     buffer_after: int = 0) -> bool:
        """Atomically save ``booking`` unless it collides with another confirmed booking
        of the same host (buffers included). Also used for reschedules (upsert)."""
        from ..scheduling.slots import is_free

        async with self._lock_for(booking.host_id):
            pad = timedelta(minutes=buffer_before + buffer_after)
            others = await self.list_bookings(host_id=booking.host_id, start=booking.start - pad,
                                              end=booking.end + pad, status=BookingStatus.CONFIRMED)
            busy = [(b.start, b.end) for b in others if b.id != booking.id]
            if not is_free(booking.start, booking.end, busy, buffer_before=buffer_before,
                           buffer_after=buffer_after):
                return False
            await self.save_booking(booking)
            return True

    async def mark_reminder_sent(self, booking_id: str, minutes: int) -> bool:
        """Claim a reminder. Returns False if another worker already sent it."""
        async with self._lock_for("reminder:" + booking_id):
            b = await self.get_booking(booking_id)
            if b is None or minutes in b.reminders_sent:
                return False
            b.reminders_sent = sorted(set(b.reminders_sent) | {minutes})
            await self.save_booking(b)
            return True

    # -- multi-tenant API keys (optional) --------------------------------
    async def save_api_key(self, key: "ApiKey") -> None:
        raise NotImplementedError("this storage backend does not support API keys")

    async def get_api_key(self, key_id: str) -> Optional["ApiKey"]:
        raise NotImplementedError("this storage backend does not support API keys")

    async def get_api_key_by_hash(self, key_hash: str) -> Optional["ApiKey"]:
        raise NotImplementedError("this storage backend does not support API keys")

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List["ApiKey"]:
        raise NotImplementedError("this storage backend does not support API keys")

    # -- stored (per-tenant) webhook endpoints (optional) ----------------
    async def save_webhook(self, endpoint: "WebhookEndpoint") -> None:
        raise NotImplementedError("this storage backend does not support stored webhooks")

    async def delete_webhook(self, webhook_id: str) -> None:
        raise NotImplementedError("this storage backend does not support stored webhooks")

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List["WebhookEndpoint"]:
        return []

    async def delete_chat(self, room_id: str, message_id: str) -> None:
        """Remove one chat message (optional; default keeps history as is)."""
        return None

    # -- roles & branding (optional; nodemeet keeps them in memory otherwise) --
    async def save_role(self, role: "RoleDefinition") -> None:
        raise NotImplementedError

    async def list_roles(self, tenant_id: Optional[str] = None) -> List["RoleDefinition"]:
        """Roles stored for exactly this tenant (None = global roles)."""
        raise NotImplementedError

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        raise NotImplementedError

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        """``key`` is a tenant id, or ``"_default"`` for the platform-wide branding."""
        raise NotImplementedError

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    # -- generic records ---------------------------------------------------------------
    # A small document store every backend provides. nodemeet keeps integration data in
    # it (calendar connections, recordings, polls, transcripts, payments, SSO state...).
    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def delete_record(self, kind: str, key: str) -> None:
        raise NotImplementedError

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        """(key, value) pairs of one kind, sorted by key."""
        raise NotImplementedError


def _matches(b: Booking, host_id: Optional[str], start: Optional[datetime],
             end: Optional[datetime], status: Optional[BookingStatus]) -> bool:
    if host_id is not None and b.host_id != host_id:
        return False
    if status is not None and b.status != BookingStatus(status):
        return False
    if start is not None and b.end <= start:
        return False
    if end is not None and b.start >= end:
        return False
    return True
