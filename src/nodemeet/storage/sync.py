"""Use a *synchronous* storage (Django ORM, sync SQLAlchemy, a REST client...).

Write a plain class with the same method names as :class:`Storage`, but
``def`` instead of ``async def``, and wrap it::

    class DjangoStorage:
        def save_room(self, room): Room.objects.update_or_create(id=room.id, defaults={"data": room.to_dict()})
        def get_room(self, room_id): ...
        ...

    meet = NodeMeet(secret, storage=SyncStorageAdapter(DjangoStorage()))

Each call runs in a thread so the event loop never blocks. Methods you don't
implement fall back to nodemeet's defaults (e.g. ``insert_booking_if_free``).
For Django you may want ``asgiref.sync.sync_to_async`` semantics; pass
``runner=sync_to_async`` style callables via ``call``.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Optional

from .base import Storage

_METHODS = (
    "setup", "close", "save_room", "get_room", "delete_room", "list_rooms", "save_availability",
    "get_availability", "save_booking", "get_booking", "list_bookings", "save_chat", "list_chat",
    "insert_booking_if_free", "mark_reminder_sent", "save_api_key", "get_api_key",
    "get_api_key_by_hash", "list_api_keys", "save_webhook", "delete_webhook", "list_webhooks",
    "delete_chat", "save_role", "list_roles", "delete_role", "save_branding", "get_branding",
    "put_record", "get_record", "delete_record", "list_records",
)


class SyncStorageAdapter(Storage):
    def __init__(self, impl: Any, *, call: Optional[Callable[..., Any]] = None) -> None:
        self.impl = impl
        self._call = call  # e.g. asgiref.sync.sync_to_async(thread_sensitive=True)
        for name in _METHODS:
            fn = getattr(impl, name, None)
            if callable(fn):
                setattr(self, name, self._wrap(fn))

    def _wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def runner(*args: Any, **kwargs: Any) -> Any:
            if self._call is not None:
                return await self._call(fn)(*args, **kwargs)
            return await asyncio.to_thread(fn, *args, **kwargs)
        return runner

    # Abstract methods must exist on the class; instances override them in __init__.
    async def save_room(self, room): raise NotImplementedError("impl lacks save_room")  # noqa: E704
    async def get_room(self, room_id): raise NotImplementedError("impl lacks get_room")  # noqa: E704
    async def delete_room(self, room_id): raise NotImplementedError("impl lacks delete_room")  # noqa: E704
    async def list_rooms(self): raise NotImplementedError("impl lacks list_rooms")  # noqa: E704
    async def save_availability(self, availability): raise NotImplementedError("impl lacks save_availability")  # noqa: E704,E501
    async def get_availability(self, host_id): raise NotImplementedError("impl lacks get_availability")  # noqa: E704
    async def save_booking(self, booking): raise NotImplementedError("impl lacks save_booking")  # noqa: E704
    async def get_booking(self, booking_id): raise NotImplementedError("impl lacks get_booking")  # noqa: E704
    async def list_bookings(self, **kw): raise NotImplementedError("impl lacks list_bookings")  # noqa: E704
