"""Key-value storage: run nodemeet on ANY key-value database.

A backend only needs six small async operations (see :class:`KVBackend`);
:class:`KeyValueStorage` builds the whole nodemeet data model on top
(secondary indexes, atomic booking via a lease lock, reminder claims).

Built-in backends:

=====================  ==========================================  =====================
Backend                Databases                                   Install
=====================  ==========================================  =====================
``MemoryKV``           in-process (tests)                          -
``DbmKV``              dbm / gdbm / ndbm / dumb (stdlib files)     -
``JsonDirKV``          a folder of JSON files (multi-process safe) -
``RedisKV``            Redis, Valkey, KeyDB, Dragonfly, Garnet     ``redis``
``DynamoDBKV``         AWS DynamoDB, ScyllaDB Alternator           ``boto3``
``CassandraKV``        Apache Cassandra, ScyllaDB, Astra DB        ``cassandra-driver``
``EtcdKV``             etcd v3 (HTTP gateway)                      -
``ConsulKV``           HashiCorp Consul KV                         -
``LMDBKV``             LMDB (embedded, multi-process)              ``lmdb``
``FoundationDBKV``     FoundationDB                                ``foundationdb``
``S3KV``               AWS S3, MinIO, Cloudflare R2, Ceph RGW...   ``boto3``
=====================  ==========================================  =====================

Anything else (Memcached-with-listing, Riak, TiKV, Aerospike, Hazelcast,
Ignite, Couchbase KV, Azure Table/Blob, GCS, Bigtable, HBase...) is one
small class implementing :class:`KVBackend`.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import quote, unquote

from ..integrations.webhooks import WebhookEndpoint
from ..models import Booking, BookingStatus, ChatMessage, RoomConfig, to_utc
from ..permissions import RoleDefinition
from ..scheduling.availability import Availability
from ..scheduling.slots import is_free
from ..tenancy import ApiKey
from .base import Storage

T = TypeVar("T")
Pair = Tuple[str, str]


def esc(part: str) -> str:
    """Escape one key component so '|' can be used as a separator."""
    return str(part).replace("%", "%25").replace("|", "%7C")


def _ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


class KVBackend:
    """Implement these for your database. Values are str (JSON)."""

    async def setup(self) -> None: ...
    async def close(self) -> None: ...
    async def get(self, space: str, key: str) -> Optional[str]: raise NotImplementedError
    async def put(self, space: str, key: str, value: str) -> None: raise NotImplementedError
    async def delete(self, space: str, key: str) -> None: raise NotImplementedError
    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        """All (key, value) whose key starts with prefix, sorted by key."""
        raise NotImplementedError
    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        """Atomic create. This is what makes bookings safe across processes."""
        raise NotImplementedError

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        """Delete only if the current value matches (override for true atomicity)."""
        if await self.get(space, key) == value:
            await self.delete(space, key)
            return True
        return False


class NamespacedKV(KVBackend):
    """Puts every space of ``backend`` under ``<namespace>.`` (for backends with no native prefix)."""

    def __init__(self, backend: KVBackend, namespace: str) -> None:
        self.backend, self.ns = backend, namespace

    def _s(self, space: str) -> str:
        return f"{self.ns}.{space}"

    def __getattr__(self, name: str) -> Any:  # backend-specific extras (transactions, locks...)
        return getattr(self.backend, name)

    async def setup(self) -> None:
        await self.backend.setup()

    async def close(self) -> None:
        await self.backend.close()

    async def get(self, space: str, key: str) -> Optional[str]:
        return await self.backend.get(self._s(space), key)

    async def put(self, space: str, key: str, value: str) -> None:
        await self.backend.put(self._s(space), key, value)

    async def delete(self, space: str, key: str) -> None:
        await self.backend.delete(self._s(space), key)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        return await self.backend.scan(self._s(space), prefix)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        return await self.backend.put_if_absent(self._s(space), key, value)

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        return await self.backend.delete_if_equals(self._s(space), key, value)


class ThreadedKV(KVBackend):
    """Helper for backends whose client library is synchronous."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        def call() -> T:
            with self._lock:
                return fn(*args)
        return await asyncio.to_thread(call)


# -- in-process & stdlib backends ------------------------------------------------
class MemoryKV(KVBackend):
    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, str]] = {}

    async def get(self, space: str, key: str) -> Optional[str]:
        return self.data.get(space, {}).get(key)

    async def put(self, space: str, key: str, value: str) -> None:
        self.data.setdefault(space, {})[key] = value

    async def delete(self, space: str, key: str) -> None:
        self.data.get(space, {}).pop(key, None)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        return sorted((k, v) for k, v in self.data.get(space, {}).items() if k.startswith(prefix))

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        bucket = self.data.setdefault(space, {})
        if key in bucket:
            return False
        bucket[key] = value
        return True


class DbmKV(ThreadedKV):
    """Python's built-in ``dbm`` (gdbm/ndbm/dumb/sqlite3). One process at a time."""

    def __init__(self, path: str = "nodemeet.dbm") -> None:
        super().__init__()
        self.path = path
        self.db: Any = None
        self._pool: Any = None

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        # Python 3.13's default dbm is dbm.sqlite3, whose objects only work on the thread that
        # created them. So one dedicated worker thread opens the file and runs every operation.
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nodemeet-dbm")
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(*args))

    async def setup(self) -> None:
        import dbm
        if self.db is None:
            self.db = await self._run(dbm.open, self.path, "c")

    async def close(self) -> None:
        if self.db is not None:
            await self._run(self.db.close)
            self.db = None
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    @staticmethod
    def _k(space: str, key: str) -> bytes:
        return f"{space}\x00{key}".encode()

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            v = self.db.get(self._k(space, key))
            return v.decode() if v is not None else None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(self.db.__setitem__, self._k(space, key), value.encode())

    async def delete(self, space: str, key: str) -> None:
        def op() -> None:
            try:
                del self.db[self._k(space, key)]
            except KeyError:
                pass
        await self._run(op)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        head = f"{space}\x00{prefix}".encode()
        cut = len(space) + 1

        def op() -> List[Pair]:
            return sorted((k.decode()[cut:], self.db[k].decode()) for k in self.db.keys()
                          if k.startswith(head))
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            k = self._k(space, key)
            if k in self.db:
                return False
            self.db[k] = value.encode()
            return True
        return await self._run(op)


class JsonDirKV(ThreadedKV):
    """One file per record under ``root/space/``. ``put_if_absent`` uses O_EXCL, so
    several processes on one machine (or a POSIX shared filesystem) stay consistent."""

    def __init__(self, root: str = "nodemeet-data") -> None:
        super().__init__()
        self.root = root

    def _dir(self, space: str) -> str:
        d = os.path.join(self.root, quote(space, safe=""))
        os.makedirs(d, exist_ok=True)
        return d

    def _file(self, space: str, key: str) -> str:
        return os.path.join(self._dir(space), quote(key, safe="") + ".json")

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            try:
                with open(self._file(space, key), encoding="utf-8") as f:
                    return f.read()
            except FileNotFoundError:
                return None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        def op() -> None:
            path = self._file(space, key)
            tmp = f"{path}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(value)
            os.replace(tmp, path)
        await self._run(op)

    async def delete(self, space: str, key: str) -> None:
        def op() -> None:
            try:
                os.remove(self._file(space, key))
            except FileNotFoundError:
                pass
        await self._run(op)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            d, head, out = self._dir(space), quote(prefix, safe=""), []
            for name in os.listdir(d):
                if name.endswith(".json") and name.startswith(head):
                    try:
                        with open(os.path.join(d, name), encoding="utf-8") as f:
                            out.append((unquote(name[:-5]), f.read()))
                    except FileNotFoundError:
                        continue
            return sorted(out)
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            try:
                fd = os.open(self._file(space, key), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value)
            return True
        return await self._run(op)


class KeyValueStorage(Storage):
    """Full nodemeet storage on top of any :class:`KVBackend`."""

    LOCK_TTL = 10.0

    def __init__(self, backend: KVBackend) -> None:
        self.kv = backend

    async def setup(self) -> None:
        await self.kv.setup()

    async def close(self) -> None:
        await self.kv.close()

    async def _json(self, space: str, key: str) -> Optional[Dict[str, Any]]:
        raw = await self.kv.get(space, key)
        return json.loads(raw) if raw is not None else None

    # rooms / availability ----------------------------------------------------------
    async def save_room(self, room: RoomConfig) -> None:
        await self.kv.put("rooms", room.id, json.dumps(room.to_dict()))

    async def get_room(self, room_id: str) -> Optional[RoomConfig]:
        d = await self._json("rooms", room_id)
        return RoomConfig.from_dict(d) if d else None

    async def delete_room(self, room_id: str) -> None:
        await self.kv.delete("rooms", room_id)
        for k, _ in await self.kv.scan("chat", esc(room_id) + "|"):
            await self.kv.delete("chat", k)

    async def list_rooms(self) -> List[RoomConfig]:
        return [RoomConfig.from_dict(json.loads(v)) for _, v in await self.kv.scan("rooms")]

    async def save_availability(self, availability: Availability) -> None:
        await self.kv.put("availability", availability.host_id, json.dumps(availability.to_dict()))

    async def get_availability(self, host_id: str) -> Optional[Availability]:
        d = await self._json("availability", host_id)
        return Availability.from_dict(d) if d else None

    # bookings (primary record + two sorted indexes) --------------------------------------
    @staticmethod
    def _idx(b: Booking) -> Tuple[str, str]:
        start = f"{_ms(b.start):015d}"
        return f"{esc(b.host_id)}|{start}|{b.id}", f"{start}|{b.id}"

    async def save_booking(self, booking: Booking) -> None:
        old = await self._json("bookings", booking.id)
        if old:
            ob = Booking.from_dict(old)
            h, t = self._idx(ob)
            await self.kv.delete("bookings_by_host", h)
            await self.kv.delete("bookings_by_time", t)
        raw = json.dumps(booking.to_dict())
        await self.kv.put("bookings", booking.id, raw)
        h, t = self._idx(booking)
        await self.kv.put("bookings_by_host", h, raw)
        await self.kv.put("bookings_by_time", t, raw)

    async def get_booking(self, booking_id: str) -> Optional[Booking]:
        d = await self._json("bookings", booking_id)
        return Booking.from_dict(d) if d else None

    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        rows = (await self.kv.scan("bookings_by_host", esc(host_id) + "|") if host_id is not None
                else await self.kv.scan("bookings_by_time"))
        s = to_utc(start) if start else None
        e = to_utc(end) if end else None
        out = []
        for _, raw in rows:
            b = Booking.from_dict(json.loads(raw))
            if e is not None and b.start >= e:
                break  # indexes are sorted by start time
            if s is not None and b.end <= s:
                continue
            if status is not None and b.status != BookingStatus(status):
                continue
            out.append(b)
        return out

    async def _acquire(self, name: str) -> str:
        token = uuid.uuid4().hex
        deadline = time.monotonic() + 15
        while True:
            value = f"{token}|{time.time() + self.LOCK_TTL}"
            if await self.kv.put_if_absent("locks", name, value):
                return value
            current = await self.kv.get("locks", name)
            if current and float(current.split("|")[1]) < time.time():  # stale lease
                await self.kv.delete_if_equals("locks", name, current)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not lock {name}")
            await asyncio.sleep(0.02)

    async def insert_booking_if_free(self, booking: Booking, *, buffer_before: int = 0,
                                     buffer_after: int = 0) -> bool:
        lease = await self._acquire("host:" + booking.host_id)
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
            await self.kv.delete_if_equals("locks", "host:" + booking.host_id, lease)

    async def mark_reminder_sent(self, booking_id: str, minutes: int) -> bool:
        if not await self.kv.put_if_absent("reminders", f"{booking_id}|{int(minutes)}", "1"):
            return False
        b = await self.get_booking(booking_id)
        if b is not None:
            b.reminders_sent = sorted(set(b.reminders_sent) | {int(minutes)})
            await self.save_booking(b)
        return True

    # chat -------------------------------------------------------------------------
    async def save_chat(self, message: ChatMessage) -> None:
        key = f"{esc(message.room)}|{message.ts:020.6f}|{message.id}"
        await self.kv.put("chat", key, json.dumps(message.to_dict()))

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        rows = await self.kv.scan("chat", esc(room_id) + "|")
        return [ChatMessage.from_dict(json.loads(v)) for _, v in rows[-limit:]]

    async def delete_chat(self, room_id: str, message_id: str) -> None:
        for k, _ in await self.kv.scan("chat", esc(room_id) + "|"):
            if k.endswith("|" + message_id):
                await self.kv.delete("chat", k)

    # api keys / webhooks / roles / branding ------------------------------------------------
    async def save_api_key(self, key: ApiKey) -> None:
        await self.kv.put("api_keys", key.id, json.dumps(key.to_dict()))
        await self.kv.put("api_key_hashes", key.key_hash, key.id)

    async def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        d = await self._json("api_keys", key_id)
        return ApiKey.from_dict(d) if d else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        kid = await self.kv.get("api_key_hashes", key_hash)
        return await self.get_api_key(kid) if kid else None

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List[ApiKey]:
        keys = [ApiKey.from_dict(json.loads(v)) for _, v in await self.kv.scan("api_keys")]
        return [k for k in keys if tenant_id is None or k.tenant_id == tenant_id]

    async def save_webhook(self, endpoint: WebhookEndpoint) -> None:
        await self.kv.put("webhooks", endpoint.id, json.dumps(endpoint.to_dict()))

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.kv.delete("webhooks", webhook_id)

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List[WebhookEndpoint]:
        items = [WebhookEndpoint.from_dict(json.loads(v)) for _, v in await self.kv.scan("webhooks")]
        return [w for w in items if tenant_id is None or w.tenant_id == tenant_id]

    async def save_role(self, role: RoleDefinition) -> None:
        await self.kv.put("roles", f"{esc(role.tenant_id or '')}|{role.name}", json.dumps(role.to_dict()))

    async def list_roles(self, tenant_id: Optional[str] = None) -> List[RoleDefinition]:
        rows = await self.kv.scan("roles", esc(tenant_id or "") + "|")
        return [RoleDefinition.from_dict(json.loads(v)) for _, v in rows]

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        await self.kv.delete("roles", f"{esc(tenant_id or '')}|{name}")

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        await self.kv.put("branding", key, json.dumps(branding))

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        return await self._json("branding", key)

    # generic records ------------------------------------------------------------------
    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        await self.kv.put("records", f"{esc(kind)}|{key}", json.dumps(value))

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        return await self._json("records", f"{esc(kind)}|{key}")

    async def delete_record(self, kind: str, key: str) -> None:
        await self.kv.delete("records", f"{esc(kind)}|{key}")

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        head = f"{esc(kind)}|"
        rows = await self.kv.scan("records", head + prefix)
        return [(k[len(head):], json.loads(v)) for k, v in rows]
