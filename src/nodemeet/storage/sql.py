"""SQLAlchemy 2.x (async) storage: Postgres, MySQL/MariaDB, SQLite, and anything
SQLAlchemy supports. ``pip install "nodemeet[sql]"`` plus your async driver
(asyncpg, aiomysql, aiosqlite...).

    storage = SQLAlchemyStorage("postgresql+asyncpg://user:pw@db/app")
    # or share your app's engine:
    storage = SQLAlchemyStorage(engine=my_async_engine, table_prefix="nm_")

Tables are created/upgraded by ``await storage.setup()`` (called on server
start) or ``nodemeet db upgrade --url ...``. Using Alembic already? Add
``nodemeet.storage.sql.metadata`` to ``target_metadata`` and let Alembic own
the DDL (pass ``create_tables=False``).

Concurrency: booking inserts lock a per-host row (``UPDATE nm_host_locks``),
so any number of workers/servers can book the same host without double
bookings. Reminder claims use a unique key, so each reminder is sent once.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import sqlalchemy as sa
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
except ImportError as exc:  # pragma: no cover
    raise ImportError('SQLAlchemyStorage needs SQLAlchemy 2: pip install "nodemeet[sql]"') from exc

from ..integrations.webhooks import WebhookEndpoint
from ..models import Booking, BookingStatus, ChatMessage, RoomConfig, to_utc
from ..scheduling.availability import Availability
from ..permissions import RoleDefinition
from ..tenancy import ApiKey
from .base import Storage

SCHEMA_VERSION = 3  # v2 adds roles + branding (create_all adds new tables)


def _ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


def build_metadata(prefix: str = "nm_") -> sa.MetaData:
    md = sa.MetaData()
    S = sa.String  # noqa: N806
    sa.Table(f"{prefix}rooms", md, sa.Column("id", S(191), primary_key=True),
             sa.Column("tenant_id", S(191), index=True), sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}availability", md, sa.Column("host_id", S(191), primary_key=True),
             sa.Column("tenant_id", S(191), index=True), sa.Column("data", sa.Text, nullable=False))
    b = sa.Table(f"{prefix}bookings", md, sa.Column("id", S(64), primary_key=True),
                 sa.Column("host_id", S(191), nullable=False),
                 sa.Column("tenant_id", S(191), index=True),
                 sa.Column("start_ms", sa.BigInteger, nullable=False),
                 sa.Column("end_ms", sa.BigInteger, nullable=False),
                 sa.Column("status", S(16), nullable=False), sa.Column("data", sa.Text, nullable=False))
    sa.Index(f"{prefix}bookings_host_start", b.c.host_id, b.c.start_ms)
    sa.Index(f"{prefix}bookings_start", b.c.start_ms)
    sa.Table(f"{prefix}host_locks", md, sa.Column("host_id", S(191), primary_key=True),
             sa.Column("n", sa.BigInteger, nullable=False, default=0))
    sa.Table(f"{prefix}reminders", md, sa.Column("booking_id", S(64), primary_key=True),
             sa.Column("minutes", sa.Integer, primary_key=True, autoincrement=False))
    sa.Table(f"{prefix}chat", md, sa.Column("seq", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                                            primary_key=True, autoincrement=True),
             sa.Column("room", S(191), nullable=False, index=True), sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}api_keys", md, sa.Column("id", S(64), primary_key=True),
             sa.Column("tenant_id", S(191), nullable=False, index=True),
             sa.Column("key_hash", S(64), nullable=False, unique=True),
             sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}webhooks", md, sa.Column("id", S(64), primary_key=True),
             sa.Column("tenant_id", S(191), index=True), sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}roles", md, sa.Column("tenant_key", S(191), primary_key=True),
             sa.Column("name", S(64), primary_key=True), sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}branding", md, sa.Column("key", S(191), primary_key=True),
             sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}records", md, sa.Column("kind", S(64), primary_key=True),
             sa.Column("rkey", S(191), primary_key=True), sa.Column("data", sa.Text, nullable=False))
    sa.Table(f"{prefix}schema_version", md, sa.Column("version", sa.Integer, nullable=False))
    return md


metadata = build_metadata()  # for Alembic autogenerate


class SQLAlchemyStorage(Storage):
    def __init__(self, url: Optional[str] = None, *, engine: Optional[AsyncEngine] = None,
                 table_prefix: str = "nm_", create_tables: bool = True,
                 engine_kwargs: Optional[Dict[str, Any]] = None) -> None:
        if engine is None and not url:
            raise ValueError("pass a database url or an AsyncEngine")
        self._own_engine = engine is None
        self.engine: AsyncEngine = engine or create_async_engine(url, **(engine_kwargs or {}))
        self.metadata = metadata if table_prefix == "nm_" else build_metadata(table_prefix)
        self.t = {name[len(table_prefix):]: tbl for name, tbl in self.metadata.tables.items()}
        self.create_tables = create_tables

    async def setup(self) -> None:
        if not self.create_tables:
            return
        async with self.engine.begin() as conn:
            await conn.run_sync(self.metadata.create_all)
            ver = self.t["schema_version"]
            current = (await conn.execute(sa.select(sa.func.max(ver.c.version)))).scalar()
            if not current:
                await conn.execute(ver.insert().values(version=SCHEMA_VERSION))
            elif current < SCHEMA_VERSION:
                await conn.execute(ver.update().values(version=SCHEMA_VERSION))
            # future versions: run incremental upgrades here, then bump the row

    async def close(self) -> None:
        if self._own_engine:
            await self.engine.dispose()

    # -- helpers -------------------------------------------------------------
    async def _upsert(self, conn: Any, table: sa.Table, key: str, values: Dict[str, Any]) -> None:
        res = await conn.execute(table.update().where(table.c[key] == values[key]).values(**values))
        if res.rowcount == 0:
            await conn.execute(table.insert().values(**values))

    async def _save(self, table: str, key: str, values: Dict[str, Any]) -> None:
        tbl = self.t[table]
        try:
            async with self.engine.begin() as conn:
                await self._upsert(conn, tbl, key, values)
        except IntegrityError:  # lost an insert race: the row exists now, update it
            async with self.engine.begin() as conn:
                await conn.execute(tbl.update().where(tbl.c[key] == values[key]).values(**values))

    async def _one(self, table: str, **where: Any) -> Optional[str]:
        tbl = self.t[table]
        stmt = sa.select(tbl.c.data)
        for col, val in where.items():
            stmt = stmt.where(tbl.c[col] == val)
        async with self.engine.connect() as conn:
            return (await conn.execute(stmt)).scalar()

    async def _all(self, stmt: Any) -> List[str]:
        async with self.engine.connect() as conn:
            return [r[0] for r in (await conn.execute(stmt)).all()]

    # -- rooms ---------------------------------------------------------------
    async def save_room(self, room: RoomConfig) -> None:
        await self._save("rooms", "id", {"id": room.id, "tenant_id": room.tenant_id,
                                         "data": json.dumps(room.to_dict())})

    async def get_room(self, room_id: str) -> Optional[RoomConfig]:
        d = await self._one("rooms", id=room_id)
        return RoomConfig.from_dict(json.loads(d)) if d else None

    async def delete_room(self, room_id: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(self.t["rooms"].delete().where(self.t["rooms"].c.id == room_id))
            await conn.execute(self.t["chat"].delete().where(self.t["chat"].c.room == room_id))

    async def list_rooms(self) -> List[RoomConfig]:
        t = self.t["rooms"]
        return [RoomConfig.from_dict(json.loads(d)) for d in await self._all(sa.select(t.c.data).order_by(t.c.id))]

    # -- availability ------------------------------------------------------------
    async def save_availability(self, availability: Availability) -> None:
        await self._save("availability", "host_id", {
            "host_id": availability.host_id, "tenant_id": availability.tenant_id,
            "data": json.dumps(availability.to_dict())})

    async def get_availability(self, host_id: str) -> Optional[Availability]:
        d = await self._one("availability", host_id=host_id)
        return Availability.from_dict(json.loads(d)) if d else None

    # -- bookings ----------------------------------------------------------------
    @staticmethod
    def _brow(b: Booking) -> Dict[str, Any]:
        return {"id": b.id, "host_id": b.host_id, "tenant_id": b.tenant_id,
                "start_ms": _ms(b.start), "end_ms": _ms(b.end), "status": b.status.value,
                "data": json.dumps(b.to_dict())}

    async def save_booking(self, booking: Booking) -> None:
        await self._save("bookings", "id", self._brow(booking))

    async def get_booking(self, booking_id: str) -> Optional[Booking]:
        d = await self._one("bookings", id=booking_id)
        return Booking.from_dict(json.loads(d)) if d else None

    async def list_bookings(self, *, host_id: Optional[str] = None,
                            start: Optional[datetime] = None, end: Optional[datetime] = None,
                            status: Optional[BookingStatus] = None) -> List[Booking]:
        t = self.t["bookings"]
        stmt = sa.select(t.c.data)
        if host_id is not None:
            stmt = stmt.where(t.c.host_id == host_id)
        if status is not None:
            stmt = stmt.where(t.c.status == BookingStatus(status).value)
        if start is not None:
            stmt = stmt.where(t.c.end_ms > _ms(start))
        if end is not None:
            stmt = stmt.where(t.c.start_ms < _ms(end))
        rows = await self._all(stmt.order_by(t.c.start_ms))
        return [Booking.from_dict(json.loads(d)) for d in rows]

    async def _ensure_host_lock_row(self, host_id: str) -> None:
        locks = self.t["host_locks"]
        try:
            async with self.engine.begin() as conn:
                exists = (await conn.execute(sa.select(locks.c.host_id)
                                             .where(locks.c.host_id == host_id))).scalar()
                if not exists:
                    await conn.execute(locks.insert().values(host_id=host_id, n=0))
        except IntegrityError:
            pass  # created concurrently by another worker

    async def insert_booking_if_free(self, booking: Booking, *, buffer_before: int = 0,
                                     buffer_after: int = 0) -> bool:
        await self._ensure_host_lock_row(booking.host_id)
        t, locks = self.t["bookings"], self.t["host_locks"]
        s, e = _ms(booking.start), _ms(booking.end)
        bb, ba = buffer_before * 60_000, buffer_after * 60_000
        async with self.engine.begin() as conn:
            # Row lock (Postgres/MySQL) or write lock (SQLite) held until commit:
            # serialises bookings for this host across every worker and server.
            await conn.execute(locks.update().where(locks.c.host_id == booking.host_id)
                               .values(n=locks.c.n + 1))
            clash = (await conn.execute(
                sa.select(t.c.id).where(
                    t.c.host_id == booking.host_id, t.c.status == "confirmed", t.c.id != booking.id,
                    sa.or_(sa.and_(t.c.start_ms < e + ba, t.c.end_ms > s - bb),
                           sa.and_(t.c.start_ms < e + bb, t.c.end_ms > s - ba))).limit(1))).first()
            if clash:
                return False  # context manager commits the harmless counter bump
            await self._upsert(conn, t, "id", self._brow(booking))
            return True

    async def mark_reminder_sent(self, booking_id: str, minutes: int) -> bool:
        r = self.t["reminders"]
        try:
            async with self.engine.begin() as conn:
                await conn.execute(r.insert().values(booking_id=booking_id, minutes=int(minutes)))
        except IntegrityError:
            return False
        b = await self.get_booking(booking_id)
        if b is not None:
            b.reminders_sent = sorted(set(b.reminders_sent) | {int(minutes)})
            await self.save_booking(b)
        return True

    # -- chat --------------------------------------------------------------------
    async def save_chat(self, message: ChatMessage) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(self.t["chat"].insert().values(room=message.room,
                                                               data=json.dumps(message.to_dict())))

    async def list_chat(self, room_id: str, limit: int = 100) -> List[ChatMessage]:
        c = self.t["chat"]
        rows = await self._all(sa.select(c.c.data).where(c.c.room == room_id)
                               .order_by(c.c.seq.desc()).limit(limit))
        return [ChatMessage.from_dict(json.loads(d)) for d in reversed(rows)]

    # -- api keys & webhooks ---------------------------------------------------------
    async def save_api_key(self, key: ApiKey) -> None:
        await self._save("api_keys", "id", {"id": key.id, "tenant_id": key.tenant_id,
                                            "key_hash": key.key_hash, "data": json.dumps(key.to_dict())})

    async def get_api_key(self, key_id: str) -> Optional[ApiKey]:
        d = await self._one("api_keys", id=key_id)
        return ApiKey.from_dict(json.loads(d)) if d else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        d = await self._one("api_keys", key_hash=key_hash)
        return ApiKey.from_dict(json.loads(d)) if d else None

    async def list_api_keys(self, tenant_id: Optional[str] = None) -> List[ApiKey]:
        k = self.t["api_keys"]
        stmt = sa.select(k.c.data).order_by(k.c.id)
        if tenant_id is not None:
            stmt = stmt.where(k.c.tenant_id == tenant_id)
        return [ApiKey.from_dict(json.loads(d)) for d in await self._all(stmt)]

    async def save_webhook(self, endpoint: WebhookEndpoint) -> None:
        await self._save("webhooks", "id", {"id": endpoint.id, "tenant_id": endpoint.tenant_id,
                                            "data": json.dumps(endpoint.to_dict())})

    async def delete_webhook(self, webhook_id: str) -> None:
        w = self.t["webhooks"]
        async with self.engine.begin() as conn:
            await conn.execute(w.delete().where(w.c.id == webhook_id))

    async def list_webhooks(self, tenant_id: Optional[str] = None) -> List[WebhookEndpoint]:
        w = self.t["webhooks"]
        stmt = sa.select(w.c.data).order_by(w.c.id)
        if tenant_id is not None:
            stmt = stmt.where(w.c.tenant_id == tenant_id)
        return [WebhookEndpoint.from_dict(json.loads(d)) for d in await self._all(stmt)]

    # -- chat deletion, roles, branding --------------------------------------------------
    async def delete_chat(self, room_id: str, message_id: str) -> None:
        c = self.t["chat"]
        async with self.engine.begin() as conn:
            rows = (await conn.execute(sa.select(c.c.seq, c.c.data).where(c.c.room == room_id))).all()
            for seq, data in rows:
                if json.loads(data).get("id") == message_id:
                    await conn.execute(c.delete().where(c.c.seq == seq))

    async def save_role(self, role: RoleDefinition) -> None:
        t = self.t["roles"]
        key = role.tenant_id or ""
        async with self.engine.begin() as conn:
            res = await conn.execute(t.update().where(t.c.tenant_key == key, t.c.name == role.name)
                                     .values(data=json.dumps(role.to_dict())))
            if res.rowcount == 0:
                await conn.execute(t.insert().values(tenant_key=key, name=role.name,
                                                     data=json.dumps(role.to_dict())))

    async def list_roles(self, tenant_id: Optional[str] = None) -> List[RoleDefinition]:
        t = self.t["roles"]
        rows = await self._all(sa.select(t.c.data).where(t.c.tenant_key == (tenant_id or ""))
                               .order_by(t.c.name))
        return [RoleDefinition.from_dict(json.loads(d)) for d in rows]

    async def delete_role(self, name: str, tenant_id: Optional[str] = None) -> None:
        t = self.t["roles"]
        async with self.engine.begin() as conn:
            await conn.execute(t.delete().where(t.c.tenant_key == (tenant_id or ""), t.c.name == name))

    async def save_branding(self, key: str, branding: Dict[str, Any]) -> None:
        await self._save("branding", "key", {"key": key, "data": json.dumps(branding)})

    async def get_branding(self, key: str) -> Optional[Dict[str, Any]]:
        d = await self._one("branding", key=key)
        return json.loads(d) if d else None

    async def put_record(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        t = self.t["records"]
        async with self.engine.begin() as conn:
            res = await conn.execute(t.update().where(t.c.kind == kind, t.c.rkey == key)
                                     .values(data=json.dumps(value)))
            if res.rowcount == 0:
                await conn.execute(t.insert().values(kind=kind, rkey=key, data=json.dumps(value)))

    async def get_record(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        t = self.t["records"]
        rows = await self._all(sa.select(t.c.data).where(t.c.kind == kind, t.c.rkey == key))
        return json.loads(rows[0]) if rows else None

    async def delete_record(self, kind: str, key: str) -> None:
        t = self.t["records"]
        async with self.engine.begin() as conn:
            await conn.execute(t.delete().where(t.c.kind == kind, t.c.rkey == key))

    async def list_records(self, kind: str, prefix: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        t = self.t["records"]
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(t.c.rkey, t.c.data).where(t.c.kind == kind)
                                       .order_by(t.c.rkey))).all()
        return [(k, json.loads(d)) for k, d in rows if k.startswith(prefix)]
