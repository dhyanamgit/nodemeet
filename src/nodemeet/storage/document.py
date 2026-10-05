"""Document-database storage.

A backend implements a handful of operations over collections of flat
documents (:class:`DocBackend`); :class:`DocumentStorage` maps nodemeet's
data model onto it. Queries only ever use equality filters plus one range
field and one sort field, which every document database supports.

Built-in backends (``nodemeet.storage.doc_backends``): MongoDB (+ Azure Cosmos
DB Mongo API, AWS DocumentDB, FerretDB), CouchDB (+ Couchbase Sync Gateway
style, PouchDB server, IBM Cloudant), Google Firestore, Elasticsearch /
OpenSearch, Neo4j (graph: documents are nodes), ArangoDB (multi-model).
``MemoryDocs`` is for tests.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..integrations.webhooks import WebhookEndpoint
from ..models import Booking, BookingStatus, ChatMessage, RoomConfig, to_utc
from ..permissions import RoleDefinition
from ..scheduling.availability import Availability
from ..scheduling.slots import is_free
from ..tenancy import ApiKey
from .base import Storage

Doc = Dict[str, Any]
Range = Tuple[str, Optional[Any], Optional[Any]]  # (field, gte, lt)

COLLECTIONS = {  # collection -> fields used in queries (create indexes on these)
    "nm_rooms": [], "nm_availability": [], "nm_bookings": ["host_id", "status", "start_ms"],
    "nm_chat": ["room", "ts"], "nm_api_keys": ["tenant_id", "key_hash"],
    "nm_webhooks": ["tenant_id"], "nm_roles": ["tenant_key"], "nm_branding": [],
    "nm_reminders": [], "nm_locks": [], "nm_records": ["kind"],
}


def _ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


class DocBackend:
    async def setup(self) -> None: ...
    async def close(self) -> None: ...
    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        """Create; return False if the id already exists (atomic)."""
        raise NotImplementedError
    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None: raise NotImplementedError
    async def get(self, coll: str, doc_id: str) -> Optional[Doc]: raise NotImplementedError
    async def delete(self, coll: str, doc_id: str) -> None: raise NotImplementedError
    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        raise NotImplementedError

    async def delete_if(self, coll: str, doc_id: str, field: str, value: Any) -> bool:
        doc = await self.get(coll, doc_id)
        if doc is not None and doc.get(field) == value:
            await self.delete(coll, doc_id)
            return True
        return False


class MemoryDocs(DocBackend):
    def __init__(self) -> None:
        self.colls: Dict[str, Dict[str, Doc]] = {}

    def _c(self, coll: str) -> Dict[str, Doc]:
        return self.colls.setdefault(coll, {})

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        c = self._c(coll)
        if doc_id in c:
            return False
        c[doc_id] = copy.deepcopy({**doc, "_id": doc_id})
        return True

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        self._c(coll)[doc_id] = copy.deepcopy({**doc, "_id": doc_id})

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        d = self._c(coll).get(doc_id)
        return copy.deepcopy(d) if d else None

    async def delete(self, coll: str, doc_id: str) -> None:
        self._c(coll).pop(doc_id, None)

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        out = [d for d in self._c(coll).values() if all(d.get(k) == v for k, v in (where or {}).items())]
        if range:
            f, lo, hi = range
            out = [d for d in out if (lo is None or d.get(f) >= lo) and (hi is None or d.get(f) < hi)]
        if order_by:
            out.sort(key=lambda d: d.get(order_by), reverse=desc)
        return copy.deepcopy(out[:limit] if limit else out)


class DocumentStorage(Storage):
    LOCK_TTL = 10.0

    def __init__(self, backend: DocBackend) -> None:
        self.db = backend

    async def setup(self) -> None:
        await self.db.setup()

    async def close(self) -> None:
        await self.db.close()

    @staticmethod
    def _data(doc: Optional[Doc]) -> Optional[Dict[str, Any]]:
        if not doc:
            return None
        d = doc.get("data")
        return json.loads(d) if isinstance(d, str) else d

    # rooms / availability --------------------------------------------------------
    async def save_room(self, room: RoomConfig) -> None:
        await self.db.upsert("nm_rooms", room.id, {"tenant_id": room.tenant_id, "data": room.to_dict()})

    async def get_room(self, room_id: str) -> Optional[RoomConfig]:
        d = self._data(await self.db.get("nm_rooms", room_id))
        return RoomConfig.from_dict(d) if d else None

    async def delete_room(self, room_id: str) -> None:
        await self.db.delete("nm_rooms", room_id)
        for doc in await self.db.find("nm_chat", {"room": room_id}):
            await self.db.delete("nm_chat", doc["_id"])

    async def list_rooms(self) -> List[RoomConfig]:
        return [RoomConfig.from_dict(self._data(d)) for d in await self.db.find("nm_rooms")]

    async def save_availability(self, availability: Availability) -> None:
        await self.db.upsert("nm_availability", availability.host_id,
                             {"tenant_id": availability.tenant_id, "data": availability.to_dict()})

    async def get_availability(self, host_id: str) -> Optional[Availability]:
        d = self._data(await self.db.get("nm_availability", host_id))
        return Availability.from_dict(d) if d else None

    # bookings ----------------------------------------------------------------------
    async def save_booking(self, booking: Booking) -> None:
        await self.db.upsert("nm_bookings", booking.id, {
            "host_id": booking.host_id, "tenant_id": booking.tenant_id, "status": booking.status.value,
            "start_ms": _ms(booking.start), "end_ms": _ms(booking.end), "data": booking.to_dict()})

    async def get_booking(self, booking_id: str) -> Optional[Booking]:
        d = self._data(await self.db.get("nm_bookings", booking_id))
        return Booking.from_dict(d) if d else None

    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        where: Doc = {}
        if host_id is not None:
            where["host_id"] = host_id
        if status is not None:
            where["status"] = BookingStatus(status).value
        rng = ("start_ms", None, _ms(end)) if end is not None else None
        docs = await self.db.find("nm_bookings", where, range=rng, order_by="start_ms")
        s = _ms(start) if start is not None else None
        return [Booking.from_dict(self._data(d)) for d in docs if s is None or d["end_ms"] > s]

    async def _acquire(self, name: str) -> str:
        token, deadline = uuid.uuid4().hex, time.monotonic() + 15
        while True:
            if await self.db.insert("nm_locks", name, {"token": token, "expires": time.time() + self.LOCK_TTL}):
                return token
            cur = await self.db.get("nm_locks", name)
            if cur and float(cur.get("expires", 0)) < time.time():
                await self.db.delete_if("nm_locks", name, "token", cur.get("token"))
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not lock {name}")
            await asyncio.sleep(0.02)

    async def insert_booking_if_free(self, booking: Booking, *, buffer_before: int = 0,
                                     buffer_after: int = 0) -> bool:
        name = "host:" + booking.host_id
        token = await self._acquire(name)
        try:
            pad = timedelta(minutes=buffer_before + buffer_after)
            others = await self.list_bookings(host_id=booking.host_id, start=booking.start - pad,
                                              end=booking.end + pad, status=BookingStatus.CONFIRMED)
            busy = [(b.start, b.end) for b in others if b.id != booking.id]
            if not is_free(booking.start, booking.end, busy, buffer_before=buffer_before,
                           buffer_after=buffer_after):
                return False
            await self.save_booking(booking)
            return True
        finally:
            await self.db.delete_if("nm_locks", name, "token", token)

    async def mark_reminder_sent(self, booking_id: str, minutes: int) -> bool:
        if not await self.db.insert("nm_reminders", f"{booking_id}:{int(minutes)}", {"at": time.time()}):
            return False
        b = await self.get_booking(booking_id)
        if b is not None:
            b.reminders_sent = sorted(set(b.reminders_sent) | {int(minutes)})
            await self.save_booking(b)
        return True

    # chat --------------------------------------------------------------------------
    async def save_chat(self, message: ChatMessage) -> None:
        await self.db.upsert("nm_chat", message.id, {"room": message.room, "ts": message.ts,
                                                     "data": message.to_dict()})

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        docs = await self.db.find("nm_chat", {"room": room_id}, order_by="ts", desc=True, limit=limit)
        return [ChatMessage.from_dict(self._data(d)) for d in reversed(docs)]

    async def delete_chat(self, room_id: str, message_id: str) -> None:
        await self.db.delete("nm_chat", message_id)

    # keys / webhooks / roles / branding -----------------------------------------------
    async def save_api_key(self, key: ApiKey) -> None:
        await self.db.upsert("nm_api_keys", key.id, {"tenant_id": key.tenant_id, "key_hash": key.key_hash,
                                                     "data": key.to_dict()})

    async def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        d = self._data(await self.db.get("nm_api_keys", key_id))
        return ApiKey.from_dict(d) if d else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        docs = await self.db.find("nm_api_keys", {"key_hash": key_hash}, limit=1)
        return ApiKey.from_dict(self._data(docs[0])) if docs else None

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List[ApiKey]:
        where = {"tenant_id": tenant_id} if tenant_id is not None else None
        return [ApiKey.from_dict(self._data(d)) for d in await self.db.find("nm_api_keys", where)]

    async def save_webhook(self, endpoint: WebhookEndpoint) -> None:
        await self.db.upsert("nm_webhooks", endpoint.id, {"tenant_id": endpoint.tenant_id,
                                                          "data": endpoint.to_dict()})

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.db.delete("nm_webhooks", webhook_id)

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List[WebhookEndpoint]:
        where = {"tenant_id": tenant_id} if tenant_id is not None else None
        return [WebhookEndpoint.from_dict(self._data(d)) for d in await self.db.find("nm_webhooks", where)]

    async def save_role(self, role: RoleDefinition) -> None:
        tk = role.tenant_id or "_global"
        await self.db.upsert("nm_roles", f"{tk}:{role.name}", {"tenant_key": tk, "data": role.to_dict()})

    async def list_roles(self, tenant_id: Optional[str] = None) -> List[RoleDefinition]:
        docs = await self.db.find("nm_roles", {"tenant_key": tenant_id or "_global"})
        return sorted((RoleDefinition.from_dict(self._data(d)) for d in docs), key=lambda r: r.name)

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        await self.db.delete("nm_roles", f"{tenant_id or '_global'}:{name}")

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        await self.db.upsert("nm_branding", key, {"data": branding})

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        return self._data(await self.db.get("nm_branding", key))

    # generic records ------------------------------------------------------------------
    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        await self.db.upsert("nm_records", f"{kind}:{key}", {"kind": kind, "rkey": key, "data": value})

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        return self._data(await self.db.get("nm_records", f"{kind}:{key}"))

    async def delete_record(self, kind: str, key: str) -> None:
        await self.db.delete("nm_records", f"{kind}:{key}")

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        docs = await self.db.find("nm_records", {"kind": kind})
        out = [(d["rkey"], self._data(d)) for d in docs if str(d.get("rkey", "")).startswith(prefix)]
        return sorted(out, key=lambda kv: kv[0])
