"""SQLite storage using only the standard library.

* Calls run in a worker thread so the event loop is never blocked.
* Schema is versioned: ``setup()`` applies pending migrations automatically
  (``nodemeet db upgrade --sqlite FILE`` does the same from the CLI).
* ``insert_booking_if_free`` uses ``BEGIN IMMEDIATE`` so several *processes*
  sharing one database file can never double-book.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from ..integrations.webhooks import WebhookEndpoint
from ..models import Booking, BookingStatus, ChatMessage, RoomConfig, to_utc
from ..scheduling.availability import Availability
from ..permissions import RoleDefinition
from ..tenancy import ApiKey
from .base import Storage

T = TypeVar("T")

MIGRATIONS: List[List[str]] = [
    [  # v1 - nodemeet 0.1
        "CREATE TABLE IF NOT EXISTS nm_rooms (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS nm_availability (host_id TEXT PRIMARY KEY, data TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS nm_bookings (id TEXT PRIMARY KEY, host_id TEXT NOT NULL, "
        "start_ts REAL NOT NULL, end_ts REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS nm_bookings_host_start ON nm_bookings (host_id, start_ts)",
        "CREATE INDEX IF NOT EXISTS nm_bookings_start ON nm_bookings (start_ts)",
        "CREATE TABLE IF NOT EXISTS nm_chat (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "room TEXT NOT NULL, data TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS nm_chat_room ON nm_chat (room, seq)",
    ],
    [  # v2 - nodemeet 0.2: tenants, api keys, stored webhooks, reminder claims
        "ALTER TABLE nm_bookings ADD COLUMN tenant_id TEXT",
        "CREATE INDEX IF NOT EXISTS nm_bookings_tenant ON nm_bookings (tenant_id)",
        "CREATE TABLE IF NOT EXISTS nm_reminders (booking_id TEXT NOT NULL, minutes INTEGER NOT NULL, "
        "PRIMARY KEY (booking_id, minutes))",
        "CREATE TABLE IF NOT EXISTS nm_api_keys (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
        "key_hash TEXT NOT NULL UNIQUE, data TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS nm_api_keys_tenant ON nm_api_keys (tenant_id)",
        "CREATE TABLE IF NOT EXISTS nm_webhooks (id TEXT PRIMARY KEY, tenant_id TEXT, data TEXT NOT NULL)",
    ],
    [  # v3 - nodemeet 0.3: custom roles, branding, chat deletion
        "CREATE TABLE IF NOT EXISTS nm_roles (tenant_key TEXT NOT NULL, name TEXT NOT NULL, "
        "data TEXT NOT NULL, PRIMARY KEY (tenant_key, name))",
        "CREATE TABLE IF NOT EXISTS nm_branding (key TEXT PRIMARY KEY, data TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS nm_chat_room_only ON nm_chat (room)",
    ],
    [  # v4 - nodemeet 0.4: generic records (integrations, recordings, polls, ...)
        "CREATE TABLE IF NOT EXISTS nm_records (kind TEXT NOT NULL, key TEXT NOT NULL, "
        "data TEXT NOT NULL, PRIMARY KEY (kind, key))",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    conn.execute("CREATE TABLE IF NOT EXISTS nm_schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM nm_schema_version").fetchone()
    current = row[0] or 0
    if current == 0:  # a 0.1 database has tables but no version row
        has_v1 = conn.execute("SELECT name FROM sqlite_master WHERE name='nm_bookings'").fetchone()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(nm_bookings)")] if has_v1 else []
        has_roles = conn.execute("SELECT name FROM sqlite_master WHERE name='nm_roles'").fetchone()
        current = (3 if has_roles else 2) if "tenant_id" in cols else 0  # v1 is idempotent
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in MIGRATIONS[version - 1]:
                conn.execute(stmt)
            conn.execute("INSERT INTO nm_schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if current and not conn.execute("SELECT 1 FROM nm_schema_version").fetchone():
        conn.execute("INSERT INTO nm_schema_version (version) VALUES (?)", (max(current, SCHEMA_VERSION),))
    return SCHEMA_VERSION


_NM = re.compile(r"\bnm_")


class _Prefixed:
    """Wraps a connection so every ``nm_`` table / index name gets another prefix."""

    def __init__(self, conn: sqlite3.Connection, prefix: str) -> None:
        self._conn, self._prefix = conn, prefix

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(_NM.sub(self._prefix, sql), params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class SQLiteStorage(Storage):
    def __init__(self, path: str = "nodemeet.sqlite3", *, busy_timeout: float = 10.0,
                 table_prefix: str = "nm_") -> None:
        """``table_prefix`` keeps several apps / tenants apart in one file (``?namespace=`` sets it)."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", table_prefix or ""):
            raise ValueError("table_prefix may only contain letters, digits and _")
        self.path = path
        self.table_prefix = table_prefix
        self.busy_timeout = busy_timeout
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                   timeout=self.busy_timeout)
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            if self.table_prefix != "nm_":
                conn = _Prefixed(conn, self.table_prefix)  # type: ignore[assignment]
            migrate(conn)
            self._conn = conn
        return self._conn

    def _call(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            return fn(self._connect())

    async def _do(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._call, fn)

    async def _exec(self, sql: str, params: Sequence[Any] = (), fetch: bool = False) -> List[Any]:
        def run(conn: sqlite3.Connection) -> List[Any]:
            cur = conn.execute(sql, params)
            return cur.fetchall() if fetch else []
        return await self._do(run)

    async def setup(self) -> None:
        await asyncio.to_thread(self._call, lambda conn: None)

    async def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # rooms ----------------------------------------------------------------
    async def save_room(self, room: RoomConfig) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_rooms (id, data) VALUES (?, ?)",
                         (room.id, json.dumps(room.to_dict())))

    async def get_room(self, room_id: str) -> Optional[RoomConfig]:
        rows = await self._exec("SELECT data FROM nm_rooms WHERE id = ?", (room_id,), True)
        return RoomConfig.from_dict(json.loads(rows[0][0])) if rows else None

    async def delete_room(self, room_id: str) -> None:
        await self._exec("DELETE FROM nm_rooms WHERE id = ?", (room_id,))
        await self._exec("DELETE FROM nm_chat WHERE room = ?", (room_id,))

    async def list_rooms(self) -> List[RoomConfig]:
        rows = await self._exec("SELECT data FROM nm_rooms ORDER BY id", (), True)
        return [RoomConfig.from_dict(json.loads(r[0])) for r in rows]

    # availability ------------------------------------------------------------
    async def save_availability(self, availability: Availability) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_availability (host_id, data) VALUES (?, ?)",
                         (availability.host_id, json.dumps(availability.to_dict())))

    async def get_availability(self, host_id: str) -> Optional[Availability]:
        rows = await self._exec("SELECT data FROM nm_availability WHERE host_id = ?",
                                (host_id,), True)
        return Availability.from_dict(json.loads(rows[0][0])) if rows else None

    # bookings -------------------------------------------------------------------
    @staticmethod
    def _booking_row(b: Booking) -> tuple:
        return (b.id, b.host_id, b.start.timestamp(), b.end.timestamp(), b.status.value,
                json.dumps(b.to_dict()), b.tenant_id)

    _UPSERT = ("INSERT OR REPLACE INTO nm_bookings (id, host_id, start_ts, end_ts, status, data, "
               "tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?)")

    async def save_booking(self, booking: Booking) -> None:
        await self._exec(self._UPSERT, self._booking_row(booking))

    async def get_booking(self, booking_id: str) -> Optional[Booking]:
        rows = await self._exec("SELECT data FROM nm_bookings WHERE id = ?", (booking_id,), True)
        return Booking.from_dict(json.loads(rows[0][0])) if rows else None

    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        where: List[str] = []
        params: List[Any] = []
        if host_id is not None:
            where.append("host_id = ?")
            params.append(host_id)
        if status is not None:
            where.append("status = ?")
            params.append(BookingStatus(status).value)
        if start is not None:
            where.append("end_ts > ?")
            params.append(to_utc(start).timestamp())
        if end is not None:
            where.append("start_ts < ?")
            params.append(to_utc(end).timestamp())
        sql = "SELECT data FROM nm_bookings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = await self._exec(sql + " ORDER BY start_ts", params, True)
        return [Booking.from_dict(json.loads(r[0])) for r in rows]

    async def insert_booking_if_free(self, booking: Booking, *, buffer_before: int = 0,
                                     buffer_after: int = 0) -> bool:
        s, e = booking.start.timestamp(), booking.end.timestamp()
        bb, ba = buffer_before * 60, buffer_after * 60

        def tx(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")  # takes the write lock across processes
            try:
                clash = conn.execute(
                    "SELECT 1 FROM nm_bookings WHERE host_id = ? AND status = 'confirmed' AND id != ? "
                    "AND ((start_ts < ? AND end_ts > ?) OR (start_ts - ? < ? AND end_ts + ? > ?)) LIMIT 1",
                    (booking.host_id, booking.id, e + ba, s - bb, bb, e, ba, s)).fetchone()
                if clash:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(self._UPSERT, self._booking_row(booking))
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return await self._do(tx)

    async def mark_reminder_sent(self, booking_id: str, minutes: int) -> bool:
        def tx(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute("INSERT OR IGNORE INTO nm_reminders (booking_id, minutes) VALUES (?, ?)",
                                   (booking_id, int(minutes)))
                claimed = cur.rowcount == 1
                if claimed:
                    row = conn.execute("SELECT data FROM nm_bookings WHERE id = ?", (booking_id,)).fetchone()
                    if row:
                        b = Booking.from_dict(json.loads(row[0]))
                        b.reminders_sent = sorted(set(b.reminders_sent) | {int(minutes)})
                        conn.execute(self._UPSERT, self._booking_row(b))
                conn.execute("COMMIT")
                return claimed
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return await self._do(tx)

    # chat -------------------------------------------------------------------------
    async def save_chat(self, message: ChatMessage) -> None:
        await self._exec("INSERT INTO nm_chat (room, data) VALUES (?, ?)",
                         (message.room, json.dumps(message.to_dict())))

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        rows = await self._exec(
            "SELECT data FROM (SELECT seq, data FROM nm_chat WHERE room = ? "
            "ORDER BY seq DESC LIMIT ?) ORDER BY seq", (room_id, limit), True)
        return [ChatMessage.from_dict(json.loads(r[0])) for r in rows]

    # api keys & webhooks -------------------------------------------------------------
    async def save_api_key(self, key: ApiKey) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_api_keys (id, tenant_id, key_hash, data) "
                         "VALUES (?, ?, ?, ?)", (key.id, key.tenant_id, key.key_hash,
                                                 json.dumps(key.to_dict())))

    async def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        rows = await self._exec("SELECT data FROM nm_api_keys WHERE id = ?", (key_id,), True)
        return ApiKey.from_dict(json.loads(rows[0][0])) if rows else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        rows = await self._exec("SELECT data FROM nm_api_keys WHERE key_hash = ?", (key_hash,), True)
        return ApiKey.from_dict(json.loads(rows[0][0])) if rows else None

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List[ApiKey]:
        if tenant_id is None:
            rows = await self._exec("SELECT data FROM nm_api_keys ORDER BY id", (), True)
        else:
            rows = await self._exec("SELECT data FROM nm_api_keys WHERE tenant_id = ? ORDER BY id",
                                    (tenant_id,), True)
        return [ApiKey.from_dict(json.loads(r[0])) for r in rows]

    async def save_webhook(self, endpoint: WebhookEndpoint) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_webhooks (id, tenant_id, data) VALUES (?, ?, ?)",
                         (endpoint.id, endpoint.tenant_id, json.dumps(endpoint.to_dict())))

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._exec("DELETE FROM nm_webhooks WHERE id = ?", (webhook_id,))

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List[WebhookEndpoint]:
        if tenant_id is None:
            rows = await self._exec("SELECT data FROM nm_webhooks ORDER BY id", (), True)
        else:
            rows = await self._exec("SELECT data FROM nm_webhooks WHERE tenant_id = ? ORDER BY id",
                                    (tenant_id,), True)
        return [WebhookEndpoint.from_dict(json.loads(r[0])) for r in rows]

    # chat deletion, roles, branding -------------------------------------------------
    async def delete_chat(self, room_id: str, message_id: str) -> None:
        await self._exec("DELETE FROM nm_chat WHERE room = ? AND json_extract(data, '$.id') = ?",
                         (room_id, message_id))

    async def save_role(self, role: RoleDefinition) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_roles (tenant_key, name, data) VALUES (?, ?, ?)",
                         (role.tenant_id or "", role.name, json.dumps(role.to_dict())))

    async def list_roles(self, tenant_id: Optional[str] = None) -> List[RoleDefinition]:
        rows = await self._exec("SELECT data FROM nm_roles WHERE tenant_key = ? ORDER BY name",
                                (tenant_id or "",), True)
        return [RoleDefinition.from_dict(json.loads(r[0])) for r in rows]

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        await self._exec("DELETE FROM nm_roles WHERE tenant_key = ? AND name = ?",
                         (tenant_id or "", name))

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_branding (key, data) VALUES (?, ?)",
                         (key, json.dumps(branding)))

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        rows = await self._exec("SELECT data FROM nm_branding WHERE key = ?", (key,), True)
        return json.loads(rows[0][0]) if rows else None

    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        await self._exec("INSERT OR REPLACE INTO nm_records (kind, key, data) VALUES (?, ?, ?)",
                         (kind, key, json.dumps(value)))

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        rows = await self._exec("SELECT data FROM nm_records WHERE kind = ? AND key = ?", (kind, key), True)
        return json.loads(rows[0][0]) if rows else None

    async def delete_record(self, kind: str, key: str) -> None:
        await self._exec("DELETE FROM nm_records WHERE kind = ? AND key = ?", (kind, key))

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        rows = await self._exec("SELECT key, data FROM nm_records WHERE kind = ? ORDER BY key", (kind,), True)
        return [(k, json.loads(d)) for k, d in rows if k.startswith(prefix)]
