"""One behaviour contract, run against every storage backend that can run here:
memory, SQLite, DB-API over sqlite3 (in all five PEP 249 parameter styles),
DB-API over **DuckDB** (a second real SQL engine), dbm, JSON files and documents.

The same contract runs against real Postgres/MySQL/Mongo/Redis/... in
tests/integration (docker compose) - see docs/testing.md.
"""
import asyncio
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from helpers import run
from nodemeet.integrations.webhooks import WebhookEndpoint
from nodemeet.models import Booking, BookingStatus, ChatMessage, RoomConfig
from nodemeet.permissions import RoleDefinition
from nodemeet.scheduling.availability import Availability
from nodemeet.storage import (DBAPIStorage, DbmKV, DocumentStorage, JsonDirKV, KeyValueStorage, MemoryDocs,
                              MemoryKV, MemoryStorage, SQLiteStorage)
from nodemeet.tenancy import ApiKey, generate_key, hash_key


# -- paramstyle emulation: sqlite3 underneath, but the driver *claims* another style -------
class _StyleCursor:
    def __init__(self, cur, style):
        self.cur, self.style = cur, style

    def execute(self, sql, params=()):
        if self.style == "format":
            sql, params = sql.replace("%%", "\0").replace("%s", "?").replace("\0", "%"), params
        elif self.style == "pyformat":
            names = re.findall(r"%\((\w+)\)s", sql)
            sql = re.sub(r"%\(\w+\)s", "?", sql).replace("%%", "%")
            params = [params[n] for n in names]
        elif self.style == "named":
            names = re.findall(r":(p\d+)", sql)
            sql = re.sub(r":p\d+", "?", sql)
            params = [params[n] for n in names]
        elif self.style == "numeric":
            idx = [int(n) for n in re.findall(r":(\d+)", sql)]
            sql = re.sub(r":\d+", "?", sql)
            params = [params[i - 1] for i in idx]
        return self.cur.execute(sql, params)

    def fetchall(self):
        return self.cur.fetchall()

    @property
    def rowcount(self):
        return self.cur.rowcount


class _StyleConn:
    def __init__(self, conn, style):
        self.conn, self.style = conn, style

    def cursor(self):
        return _StyleCursor(self.conn.cursor(), self.style)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def _sqlite_path():
    return os.path.join(tempfile.mkdtemp(), "nm.db")


def backends():
    out = {"memory": lambda: MemoryStorage(), "sqlite": lambda: SQLiteStorage(_sqlite_path())}
    for style in ("qmark", "format", "pyformat", "named", "numeric"):
        def make(style=style):
            path = _sqlite_path()
            return DBAPIStorage(lambda: _StyleConn(sqlite3.connect(path, timeout=10, check_same_thread=False), style),
                                dialect="sqlite", paramstyle=style)
        out[f"dbapi-sqlite-{style}"] = make
    try:
        import duckdb

        def duck():
            db = duckdb.connect(os.path.join(tempfile.mkdtemp(), "nm.duckdb"))
            return DBAPIStorage(lambda: db.cursor(), dialect="duckdb", paramstyle="qmark", pool_size=4)
        out["dbapi-duckdb"] = duck  # fixed in 0.7.1 (explicit transactions + DuckDB row counts)
    except ImportError:
        pass
    out["kv-memory"] = lambda: KeyValueStorage(MemoryKV())
    out["kv-dbm"] = lambda: KeyValueStorage(DbmKV(os.path.join(tempfile.mkdtemp(), "nm.dbm")))
    out["kv-jsondir"] = lambda: KeyValueStorage(JsonDirKV(tempfile.mkdtemp()))
    out["documents-memory"] = lambda: DocumentStorage(MemoryDocs())
    return out


T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


async def contract(st):
    await st.setup()
    try:
        # rooms
        room = RoomConfig(id="r1", name="Stand-up", tenant_id="acme", allowed_users=["ana"], banned_users=["bob"])
        await st.save_room(room)
        got = await st.get_room("r1")
        assert got.name == "Stand-up" and got.allowed_users == ["ana"] and got.is_banned("BOB")
        got.name = "Renamed"
        await st.save_room(got)
        assert (await st.get_room("r1")).name == "Renamed"
        assert [r.id for r in await st.list_rooms()] == ["r1"]
        # availability
        av = Availability.from_hours("host1", "Asia/Kolkata", {"mon-fri": "09:00-17:00"}, price=100, tenant_id="acme")
        await st.save_availability(av)
        av2 = await st.get_availability("host1")
        assert av2.timezone == "Asia/Kolkata" and av2.price == 100 and av2.weekly[0]
        # bookings + atomic insert with buffers
        b1 = Booking(host_id="host1", start=T0, end=T0 + timedelta(minutes=30), attendee_name="A", attendee_email="a@x.com")
        assert await st.insert_booking_if_free(b1, buffer_after=10)
        clash = Booking(host_id="host1", start=T0 + timedelta(minutes=35), end=T0 + timedelta(minutes=65),
                        attendee_name="B", attendee_email="b@x.com")
        assert not await st.insert_booking_if_free(clash, buffer_after=10)       # inside the 10-min buffer
        ok = Booking(host_id="host1", start=T0 + timedelta(minutes=40), end=T0 + timedelta(minutes=70),
                     attendee_name="C", attendee_email="c@x.com")
        assert await st.insert_booking_if_free(ok, buffer_after=10)
        other_host = Booking(host_id="host2", start=T0, end=T0 + timedelta(minutes=30), attendee_name="D", attendee_email="d@x.com")
        assert await st.insert_booking_if_free(other_host)
        lst = await st.list_bookings(host_id="host1", start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=3))
        assert [b.id for b in lst] == [b1.id, ok.id]
        b1.status = BookingStatus.CANCELLED
        b1.start, b1.end = T0 + timedelta(days=1), T0 + timedelta(days=1, minutes=30)
        await st.save_booking(b1)
        assert [b.id for b in await st.list_bookings(host_id="host1", status=BookingStatus.CONFIRMED)] == [ok.id]
        moved = await st.list_bookings(host_id="host1", start=T0 + timedelta(hours=20))
        assert [b.id for b in moved] == [b1.id]                                  # indexes follow updates
        assert (await st.get_booking(b1.id)).status == BookingStatus.CANCELLED
        # reminders are claimed exactly once
        assert await st.mark_reminder_sent(ok.id, 15)
        assert not await st.mark_reminder_sent(ok.id, 15)
        assert 15 in (await st.get_booking(ok.id)).reminders_sent
        # concurrency: 8 racers, one wins
        racers = [Booking(host_id="host3", start=T0, end=T0 + timedelta(minutes=30), attendee_name=f"R{i}",
                          attendee_email=f"r{i}@x.com") for i in range(8)]
        wins = await asyncio.gather(*(st.insert_booking_if_free(r) for r in racers))
        assert sum(bool(w) for w in wins) == 1, wins
        # chat
        for i in range(5):
            await st.save_chat(ChatMessage(room="r1", peer_id="p", user_id="u", name="n", text=f"m{i}", ts=1000 + i))
        msgs = await st.list_chat("r1", limit=3)
        assert [m.text for m in msgs] == ["m2", "m3", "m4"]
        await st.delete_chat("r1", msgs[0].id)
        assert [m.text for m in await st.list_chat("r1", limit=10)] == ["m0", "m1", "m3", "m4"]
        # api keys
        plain, _, _ = generate_key()
        key = ApiKey(tenant_id="acme", name="prod", key_hash=hash_key(plain), prefix=plain[:12])
        await st.save_api_key(key)
        assert (await st.get_api_key_by_hash(hash_key(plain))).id == key.id
        assert [k.id for k in await st.list_api_keys("acme")] == [key.id] and await st.list_api_keys("other") == []
        # webhooks
        wh = WebhookEndpoint(url="https://h.example", secret="s", tenant_id="acme")
        await st.save_webhook(wh)
        assert [w.id for w in await st.list_webhooks("acme")] == [wh.id]
        await st.delete_webhook(wh.id)
        assert await st.list_webhooks() == []
        # roles (global vs tenant) and branding
        await st.save_role(RoleDefinition("teacher", {"chat.*"}, {"rank": 50}))
        await st.save_role(RoleDefinition("teacher", {"chat.send"}, {"rank": 20}, tenant_id="acme"))
        assert (await st.list_roles())[0].rank == 50 and (await st.list_roles("acme"))[0].rank == 20
        await st.delete_role("teacher", "acme")
        assert await st.list_roles("acme") == [] and len(await st.list_roles()) == 1
        await st.save_branding("_default", {"name": "Acme"})
        assert (await st.get_branding("_default"))["name"] == "Acme" and await st.get_branding("nope") is None
        # generic records
        await st.put_record("calendar", "host1|google", {"a": 1})
        await st.put_record("calendar", "host1|google", {"a": 2})
        await st.put_record("calendar", "host2|outlook", {"b": 1})
        assert await st.get_record("calendar", "host1|google") == {"a": 2}
        assert [k for k, _ in await st.list_records("calendar", "host1|")] == ["host1|google"]
        await st.delete_record("calendar", "host1|google")
        assert await st.get_record("calendar", "host1|google") is None
        # deleting a room drops its chat
        await st.delete_room("r1")
        assert await st.get_room("r1") is None and await st.list_chat("r1") == []
    finally:
        await st.close()


def _make_test(name, factory):
    def test():
        run(contract(factory()))
    test.__name__ = f"test_contract_{name.replace('-', '_')}"
    return test


for _name, _factory in backends().items():
    globals()[f"test_contract_{_name.replace('-', '_')}"] = _make_test(_name, _factory)
