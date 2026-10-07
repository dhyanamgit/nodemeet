"""Plain DB-API 2.0 (PEP 249) storage: any SQL database with a Python driver,
no SQLAlchemy required.

    import psycopg
    storage = DBAPIStorage(lambda: psycopg.connect(DSN), dialect="postgres")

Dialect profiles adapt column types and row limiting; parameter style is taken
from the driver module (qmark, numeric, named, format, pyformat).

========== ===========================================================================
dialect    databases / drivers
========== ===========================================================================
sqlite     sqlite3, pysqlite3, libSQL/Turso (libsql-experimental), rqlite (pyrqlite)
postgres   psycopg 3, psycopg2, pg8000, CockroachDB, YugabyteDB, TimescaleDB, Neon,
           AlloyDB, Supabase, Citus, Greenplum, AWS Aurora/Redshift*
mysql      mysqlclient, PyMySQL, mysql-connector, MariaDB Connector, TiDB, Vitess,
           PlanetScale, SingleStore*, Percona, Aurora MySQL
mssql      pyodbc, pymssql, mssql-python: SQL Server, Azure SQL, Synapse*
oracle     oracledb / cx_Oracle: Oracle 12c+, Autonomous DB
db2        ibm_db_dbi: Db2 LUW, Db2 for i/z
duckdb     duckdb, MotherDuck (single writer)
firebird   firebird-driver, fdb (Firebird 3+)
hana       hdbcli (SAP HANA)
snowflake  snowflake-connector-python*
clickhouse clickhouse-driver dbapi* (no transactions: use for low-contention only)
generic    anything else that speaks SQL-92 with LIMIT
========== ===========================================================================

``*`` = analytical engines without row locks; bookings stay consistent for a
single writer but not under heavy concurrent booking of one host.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..integrations.webhooks import WebhookEndpoint
from ..models import Booking, BookingStatus, ChatMessage, RoomConfig, to_utc
from ..permissions import RoleDefinition
from ..scheduling.availability import Availability
from ..tenancy import ApiKey
from .base import Storage

GLOBAL = "_"  # Oracle treats '' as NULL, so "no tenant" is stored as "_"

DIALECTS: Dict[str, Dict[str, str]] = {
    "sqlite": {"text": "TEXT", "key": "VARCHAR(191)", "big": "BIGINT", "limit": "LIMIT {n}"},
    "postgres": {"text": "TEXT", "key": "VARCHAR(191)", "big": "BIGINT", "limit": "LIMIT {n}"},
    "mysql": {"text": "LONGTEXT", "key": "VARCHAR(191)", "big": "BIGINT", "limit": "LIMIT {n}"},
    "mssql": {"text": "NVARCHAR(MAX)", "key": "NVARCHAR(191)", "big": "BIGINT",
              "limit": "OFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY"},
    "oracle": {"text": "CLOB", "key": "VARCHAR2(191)", "big": "NUMBER(19)",
               "limit": "FETCH FIRST {n} ROWS ONLY"},
    "db2": {"text": "CLOB", "key": "VARCHAR(191)", "big": "BIGINT", "limit": "FETCH FIRST {n} ROWS ONLY"},
    "duckdb": {"text": "TEXT", "key": "VARCHAR", "big": "BIGINT", "limit": "LIMIT {n}"},
    "firebird": {"text": "BLOB SUB_TYPE TEXT", "key": "VARCHAR(191)", "big": "BIGINT",
                 "limit": "FETCH FIRST {n} ROWS ONLY"},
    "hana": {"text": "NCLOB", "key": "NVARCHAR(191)", "big": "BIGINT", "limit": "LIMIT {n}"},
    "snowflake": {"text": "TEXT", "key": "VARCHAR(191)", "big": "BIGINT", "limit": "LIMIT {n}"},
    "clickhouse": {"text": "String", "key": "String", "big": "Int64", "limit": "LIMIT {n}"},
    "generic": {"text": "TEXT", "key": "VARCHAR(191)", "big": "BIGINT", "limit": "LIMIT {n}"},
}


def _tables(p: str, d: Dict[str, str]) -> Dict[str, str]:
    K, T, B = d["key"], d["text"], d["big"]  # noqa: N806
    return {
        f"{p}rooms": f"id {K} NOT NULL PRIMARY KEY, tenant_id {K}, data {T} NOT NULL",
        f"{p}availability": f"host_id {K} NOT NULL PRIMARY KEY, tenant_id {K}, data {T} NOT NULL",
        f"{p}bookings": (f"id {K} NOT NULL PRIMARY KEY, host_id {K} NOT NULL, tenant_id {K}, "
                         f"start_ms {B} NOT NULL, end_ms {B} NOT NULL, status {K} NOT NULL, data {T} NOT NULL"),
        f"{p}host_locks": f"host_id {K} NOT NULL PRIMARY KEY, n {B} NOT NULL",
        f"{p}reminders": f"booking_id {K} NOT NULL, mins {B} NOT NULL, PRIMARY KEY (booking_id, mins)",
        f"{p}chat": f"room {K} NOT NULL, skey {K} NOT NULL, msg_id {K} NOT NULL, data {T} NOT NULL, PRIMARY KEY (room, skey)",
        f"{p}api_keys": f"id {K} NOT NULL PRIMARY KEY, tenant_id {K} NOT NULL, key_hash {K} NOT NULL UNIQUE, data {T} NOT NULL",
        f"{p}webhooks": f"id {K} NOT NULL PRIMARY KEY, tenant_id {K}, data {T} NOT NULL",
        f"{p}roles": f"tenant_key {K} NOT NULL, name {K} NOT NULL, data {T} NOT NULL, PRIMARY KEY (tenant_key, name)",
        f"{p}branding": f"bkey {K} NOT NULL PRIMARY KEY, data {T} NOT NULL",
        f"{p}records": f"kind {K} NOT NULL, rkey {K} NOT NULL, data {T} NOT NULL, PRIMARY KEY (kind, rkey)",
    }


INDEXES = [("bookings", "host_id, start_ms"), ("bookings", "start_ms"), ("api_keys", "tenant_id"),
           ("rooms", "tenant_id")]


def convert_params(sql: str, params: Sequence[Any], style: str) -> Tuple[str, Any]:
    """Rewrite ``?`` placeholders for the driver's paramstyle."""
    if style == "qmark":
        return sql, tuple(params)
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError("placeholder count mismatch")
    fmt = style in ("format", "pyformat")
    out, named = (parts[0].replace("%", "%%") if fmt else parts[0]), {}
    for i, part in enumerate(parts[1:], 1):
        if style == "numeric":
            out += f":{i}"
        elif style == "named":
            out += f":p{i}"
        elif style == "format":
            out += "%s"
        elif style == "pyformat":
            out += f"%(p{i})s"
        else:
            raise ValueError(f"unsupported paramstyle {style!r}")
        out += part.replace("%", "%%") if fmt else part
        named[f"p{i}"] = params[i - 1]
    if style in ("named", "pyformat"):
        return out, named
    return out, tuple(params)


def _safe_rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - e.g. DuckDB: no transaction active
        pass


RETRYABLE = ("conflict", "serializ", "deadlock", "could not serialize", "lock wait", "database is locked")


class _Tx:
    """Cursor wrapper that speaks '?' placeholders regardless of the driver."""

    def __init__(self, conn: Any, style: str, explicit: bool = False) -> None:
        self.conn, self.style, self.cur = conn, style, conn.cursor()
        self.explicit = explicit  # DuckDB: cursors are separate autocommit connections -> BEGIN/COMMIT here
        if explicit:
            self.cur.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        if self.explicit:
            self.cur.execute("COMMIT")
        else:
            self.conn.commit()

    def rollback(self) -> None:
        if self.explicit:
            self.cur.execute("ROLLBACK")
        else:
            self.conn.rollback()

    def run(self, sql: str, params: Sequence[Any] = ()) -> Any:
        q, p = convert_params(sql, params, self.style)
        self.cur.execute(q, p)
        return self.cur

    def rows(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        return list(self.run(sql, params).fetchall())

    def rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.run(sql, params)
        n = cur.rowcount
        if n is None or n < 0:  # DuckDB (and some others) report -1: the count comes back as a row
            try:
                row = cur.fetchone()
                n = int(row[0]) if row else 0
            except Exception:  # noqa: BLE001
                n = 0
        return int(n or 0)


def _text(v: Any) -> str:
    if hasattr(v, "read"):  # Oracle LOBs
        v = v.read()
    return v.decode() if isinstance(v, (bytes, bytearray, memoryview)) and not isinstance(v, str) else str(v)


class DBAPIStorage(Storage):
    def __init__(self, connect: Callable[[], Any], *, dialect: str = "generic",
                 paramstyle: Optional[str] = None, table_prefix: str = "nm_", pool_size: int = 4,
                 create_tables: bool = True) -> None:
        if dialect not in DIALECTS:
            raise ValueError(f"dialect must be one of {sorted(DIALECTS)}")
        self.connect, self.dialect, self.p = connect, dialect, table_prefix
        self.d = DIALECTS[dialect]
        self._style = paramstyle
        self.create_tables = create_tables
        self._pool: "queue.Queue[Any]" = queue.Queue(maxsize=pool_size)
        self._size = pool_size
        self._opened = 0
        self._guard = threading.Lock()

    # -- connection handling --------------------------------------------------------
    def _conn(self) -> Any:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            with self._guard:
                if self._opened < self._size:
                    self._opened += 1
                    return self.connect()
            return self._pool.get()

    def _style_of(self, conn: Any) -> str:
        if self._style:
            return self._style
        mod = __import__(type(conn).__module__.split(".")[0])
        self._style = getattr(mod, "paramstyle", None) or "qmark"
        return self._style

    def _call(self, fn: Callable[[_Tx], Any]) -> Any:
        conn = self._conn()
        try:
            tx = _Tx(conn, self._style_of(conn), explicit=self.dialect == "duckdb")
            try:
                result = fn(tx)
                tx.commit()
                return result
            except Exception:
                try:
                    tx.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            self._pool.put(conn)

    async def _do(self, fn: Callable[[_Tx], Any]) -> Any:
        """Run in a thread; retry optimistic-concurrency conflicts (DuckDB, CockroachDB,
        Postgres SERIALIZABLE, MySQL deadlocks) a few times."""
        for attempt in range(8):
            try:
                return await asyncio.to_thread(self._call, fn)
            except Exception as exc:  # noqa: BLE001
                if attempt == 7 or not any(k in str(exc).lower() for k in RETRYABLE):
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))

    def t(self, name: str) -> str:
        return self.p + name

    async def setup(self) -> None:
        if not self.create_tables:
            return

        def create(tx: _Tx) -> None:
            for table, cols in _tables(self.p, self.d).items():
                try:
                    tx.rows(f"SELECT 1 FROM {table} WHERE 1 = 0")
                    continue  # exists
                except Exception:  # noqa: BLE001
                    _safe_rollback(tx.conn)
                tx.run(f"CREATE TABLE {table} ({cols})")
                tx.conn.commit()
            for table, cols in INDEXES:
                name = f"{self.p}{table}_{cols.replace(', ', '_')}_ix"
                try:
                    tx.run(f"CREATE INDEX {name} ON {self.t(table)} ({cols})")
                    tx.conn.commit()
                except Exception:  # noqa: BLE001 - already exists
                    _safe_rollback(tx.conn)
        await self._do(create)

    async def close(self) -> None:
        while not self._pool.empty():
            try:
                self._pool.get_nowait().close()
            except Exception:  # noqa: BLE001
                pass
        self._opened = 0

    # -- generic helpers -----------------------------------------------------------------
    def _upsert(self, tx: _Tx, table: str, keys: Dict[str, Any], values: Dict[str, Any]) -> None:
        sets = ", ".join(f"{c} = ?" for c in values)
        where = " AND ".join(f"{c} = ?" for c in keys)
        n = tx.rowcount(f"UPDATE {self.t(table)} SET {sets} WHERE {where}",
                        list(values.values()) + list(keys.values()))
        if n == 0:
            cols = {**keys, **values}
            tx.run(f"INSERT INTO {self.t(table)} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   list(cols.values()))

    async def _save(self, table: str, keys: Dict[str, Any], values: Dict[str, Any]) -> None:
        await self._do(lambda tx: self._upsert(tx, table, keys, values))

    async def _one(self, table: str, where: Dict[str, Any]) -> Optional[str]:
        cond = " AND ".join(f"{c} = ?" for c in where)
        rows = await self._do(lambda tx: tx.rows(f"SELECT data FROM {self.t(table)} WHERE {cond}",
                                                 list(where.values())))
        return _text(rows[0][0]) if rows else None

    async def _many(self, sql: str, params: Sequence[Any] = ()) -> List[str]:
        rows = await self._do(lambda tx: tx.rows(sql, params))
        return [_text(r[0]) for r in rows]

    # -- rooms / availability -------------------------------------------------------------
    async def save_room(self, room: RoomConfig) -> None:
        await self._save("rooms", {"id": room.id}, {"tenant_id": room.tenant_id,
                                                    "data": json.dumps(room.to_dict())})

    async def get_room(self, room_id: str) -> Optional[RoomConfig]:
        d = await self._one("rooms", {"id": room_id})
        return RoomConfig.from_dict(json.loads(d)) if d else None

    async def delete_room(self, room_id: str) -> None:
        def op(tx: _Tx) -> None:
            tx.run(f"DELETE FROM {self.t('rooms')} WHERE id = ?", [room_id])
            tx.run(f"DELETE FROM {self.t('chat')} WHERE room = ?", [room_id])
        await self._do(op)

    async def list_rooms(self) -> List[RoomConfig]:
        return [RoomConfig.from_dict(json.loads(d))
                for d in await self._many(f"SELECT data FROM {self.t('rooms')} ORDER BY id")]

    async def save_availability(self, availability: Availability) -> None:
        await self._save("availability", {"host_id": availability.host_id},
                         {"tenant_id": availability.tenant_id, "data": json.dumps(availability.to_dict())})

    async def get_availability(self, host_id: str) -> Optional[Availability]:
        d = await self._one("availability", {"host_id": host_id})
        return Availability.from_dict(json.loads(d)) if d else None

    # -- bookings ------------------------------------------------------------------------------
    @staticmethod
    def _bvals(b: Booking) -> Dict[str, Any]:
        return {"host_id": b.host_id, "tenant_id": b.tenant_id, "start_ms": int(to_utc(b.start).timestamp() * 1000),
                "end_ms": int(to_utc(b.end).timestamp() * 1000), "status": b.status.value,
                "data": json.dumps(b.to_dict())}

    async def save_booking(self, booking: Booking) -> None:
        await self._save("bookings", {"id": booking.id}, self._bvals(booking))

    async def get_booking(self, booking_id: str) -> Optional[Booking]:
        d = await self._one("bookings", {"id": booking_id})
        return Booking.from_dict(json.loads(d)) if d else None

    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        where, params = [], []  # type: List[str], List[Any]
        if host_id is not None:
            where.append("host_id = ?")
            params.append(host_id)
        if status is not None:
            where.append("status = ?")
            params.append(BookingStatus(status).value)
        if start is not None:
            where.append("end_ms > ?")
            params.append(int(to_utc(start).timestamp() * 1000))
        if end is not None:
            where.append("start_ms < ?")
            params.append(int(to_utc(end).timestamp() * 1000))
        sql = f"SELECT data FROM {self.t('bookings')}" + (" WHERE " + " AND ".join(where) if where else "")
        return [Booking.from_dict(json.loads(d)) for d in await self._many(sql + " ORDER BY start_ms", params)]

    async def insert_booking_if_free(self, booking: Booking, *, buffer_before: int = 0,
                                     buffer_after: int = 0) -> bool:
        v = self._bvals(booking)
        s, e = v["start_ms"], v["end_ms"]
        bb, ba = buffer_before * 60_000, buffer_after * 60_000
        locks = self.t("host_locks")

        def ensure_lock_row(tx: _Tx) -> None:
            if not tx.rows(f"SELECT host_id FROM {locks} WHERE host_id = ?", [booking.host_id]):
                tx.run(f"INSERT INTO {locks} (host_id, n) VALUES (?, ?)", [booking.host_id, 0])

        try:
            await self._do(ensure_lock_row)
        except Exception:  # noqa: BLE001 - another worker created it
            pass

        def op(tx: _Tx) -> bool:
            # The UPDATE takes a row/write lock held until commit: serialises this host.
            tx.run(f"UPDATE {locks} SET n = n + 1 WHERE host_id = ?", [booking.host_id])
            clash = tx.rows(
                f"SELECT id FROM {self.t('bookings')} WHERE host_id = ? AND status = ? AND id <> ? "
                "AND ((start_ms < ? AND end_ms > ?) OR (start_ms < ? AND end_ms > ?))",
                [booking.host_id, "confirmed", booking.id, e + ba, s - bb, e + bb, s - ba])
            if clash:
                return False
            self._upsert(tx, "bookings", {"id": booking.id}, v)
            return True
        return bool(await self._do(op))

    async def mark_reminder_sent(self, booking_id: str, minutes: int) -> bool:
        def op(tx: _Tx) -> bool:
            tx.run(f"INSERT INTO {self.t('reminders')} (booking_id, mins) VALUES (?, ?)",
                   [booking_id, int(minutes)])
            return True
        try:
            await self._do(op)
        except Exception:  # noqa: BLE001 - unique violation: already claimed
            return False
        b = await self.get_booking(booking_id)
        if b is not None:
            b.reminders_sent = sorted(set(b.reminders_sent) | {int(minutes)})
            await self.save_booking(b)
        return True

    # -- chat ------------------------------------------------------------------------------------
    async def save_chat(self, message: ChatMessage) -> None:
        await self._save("chat", {"room": message.room, "skey": f"{message.ts:020.6f}|{message.id}"},
                         {"msg_id": message.id, "data": json.dumps(message.to_dict())})

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        lim = self.d["limit"].format(n=int(limit))
        rows = await self._many(f"SELECT data FROM {self.t('chat')} WHERE room = ? ORDER BY skey DESC {lim}",
                                [room_id])
        return [ChatMessage.from_dict(json.loads(d)) for d in reversed(rows)]

    async def delete_chat(self, room_id: str, message_id: str) -> None:
        await self._do(lambda tx: tx.run(f"DELETE FROM {self.t('chat')} WHERE room = ? AND msg_id = ?",
                                         [room_id, message_id]))

    # -- keys / webhooks / roles / branding ------------------------------------------------------------
    async def save_api_key(self, key: ApiKey) -> None:
        await self._save("api_keys", {"id": key.id}, {"tenant_id": key.tenant_id, "key_hash": key.key_hash,
                                                      "data": json.dumps(key.to_dict())})

    async def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        d = await self._one("api_keys", {"id": key_id})
        return ApiKey.from_dict(json.loads(d)) if d else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        d = await self._one("api_keys", {"key_hash": key_hash})
        return ApiKey.from_dict(json.loads(d)) if d else None

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List[ApiKey]:
        if tenant_id is None:
            rows = await self._many(f"SELECT data FROM {self.t('api_keys')} ORDER BY id")
        else:
            rows = await self._many(f"SELECT data FROM {self.t('api_keys')} WHERE tenant_id = ? ORDER BY id",
                                    [tenant_id])
        return [ApiKey.from_dict(json.loads(d)) for d in rows]

    async def save_webhook(self, endpoint: WebhookEndpoint) -> None:
        await self._save("webhooks", {"id": endpoint.id}, {"tenant_id": endpoint.tenant_id,
                                                           "data": json.dumps(endpoint.to_dict())})

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._do(lambda tx: tx.run(f"DELETE FROM {self.t('webhooks')} WHERE id = ?", [webhook_id]))

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List[WebhookEndpoint]:
        if tenant_id is None:
            rows = await self._many(f"SELECT data FROM {self.t('webhooks')} ORDER BY id")
        else:
            rows = await self._many(f"SELECT data FROM {self.t('webhooks')} WHERE tenant_id = ? ORDER BY id",
                                    [tenant_id])
        return [WebhookEndpoint.from_dict(json.loads(d)) for d in rows]

    async def save_role(self, role: RoleDefinition) -> None:
        await self._save("roles", {"tenant_key": role.tenant_id or GLOBAL, "name": role.name},
                         {"data": json.dumps(role.to_dict())})

    async def list_roles(self, tenant_id: Optional[str] = None) -> List[RoleDefinition]:
        rows = await self._many(f"SELECT data FROM {self.t('roles')} WHERE tenant_key = ? ORDER BY name",
                                [tenant_id or GLOBAL])
        return [RoleDefinition.from_dict(json.loads(d)) for d in rows]

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        await self._do(lambda tx: tx.run(f"DELETE FROM {self.t('roles')} WHERE tenant_key = ? AND name = ?",
                                         [tenant_id or GLOBAL, name]))

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        await self._save("branding", {"bkey": key}, {"data": json.dumps(branding)})

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        d = await self._one("branding", {"bkey": key})
        return json.loads(d) if d else None

    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        await self._save("records", {"kind": kind, "rkey": key}, {"data": json.dumps(value)})

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        d = await self._one("records", {"kind": kind, "rkey": key})
        return json.loads(d) if d else None

    async def delete_record(self, kind: str, key: str) -> None:
        await self._do(lambda tx: tx.run(f"DELETE FROM {self.t('records')} WHERE kind = ? AND rkey = ?",
                                         [kind, key]))

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        rows = await self._do(lambda tx: tx.rows(
            f"SELECT rkey, data FROM {self.t('records')} WHERE kind = ? ORDER BY rkey", [kind]))
        return [(_text(k), json.loads(_text(d))) for k, d in rows if _text(k).startswith(prefix)]
