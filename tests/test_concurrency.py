"""Double-booking and duplicate-reminder protection with many workers.

Each thread uses its *own* SQLiteStorage connection to one database file,
which is exactly the situation of several worker processes.
"""
import asyncio
import os
import tempfile
import threading
from datetime import timedelta

from helpers import run, utc
from nodemeet import Availability, Booking, BookingService, MemoryStorage, SQLiteStorage
from nodemeet.exceptions import SlotUnavailable

START = utc(2026, 10, 5, 9)


def _parallel(n, fn):
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        results[i] = asyncio.run(fn(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_sqlite_workers_cannot_double_book():
    path = os.path.join(tempfile.mkdtemp(), "shared.sqlite3")
    run(SQLiteStorage(path).setup())

    async def attempt(i):
        s = SQLiteStorage(path)
        b = Booking("h", START + timedelta(minutes=5 * (i % 2)), START + timedelta(minutes=30), f"P{i}", f"p{i}@x.test")
        ok = await s.insert_booking_if_free(b, buffer_after=10)
        await s.close()
        return ok

    results = _parallel(8, attempt)
    assert results.count(True) == 1
    rows = run(SQLiteStorage(path).list_bookings(host_id="h"))
    assert len(rows) == 1


def test_buffers_enforced_atomically_and_reschedule_upserts():
    async def scenario():
        for s in (MemoryStorage(), SQLiteStorage(":memory:")):
            await s.setup()
            a = Booking("h", START, START + timedelta(minutes=30), "A", "a@x.test")
            assert await s.insert_booking_if_free(a, buffer_after=15)
            near = Booking("h", START + timedelta(minutes=40), START + timedelta(minutes=70), "B", "b@x.test")
            assert not await s.insert_booking_if_free(near, buffer_after=15)
            far = Booking("h", START + timedelta(minutes=45), START + timedelta(minutes=75), "C", "c@x.test")
            assert await s.insert_booking_if_free(far, buffer_after=15)
            other_host = Booking("h2", START, START + timedelta(minutes=30), "D", "d@x.test")
            assert await s.insert_booking_if_free(other_host)
            a.start, a.end = START - timedelta(hours=2), START - timedelta(hours=1, minutes=30)
            assert await s.insert_booking_if_free(a, buffer_after=15)  # moving itself is fine
            assert len(await s.list_bookings(host_id="h")) == 2
            await s.close()

    run(scenario())


def test_reminder_claimed_once_across_workers():
    path = os.path.join(tempfile.mkdtemp(), "rem.sqlite3")

    async def seed():
        s = SQLiteStorage(path)
        await s.save_booking(Booking("h", START, START + timedelta(minutes=30), "A", "a@x.test", id="bk_1"))
        await s.close()

    run(seed())

    async def claim(i):
        s = SQLiteStorage(path)
        ok = await s.mark_reminder_sent("bk_1", 15)
        await s.close()
        return ok

    assert _parallel(6, claim).count(True) == 1
    assert run(SQLiteStorage(path).get_booking("bk_1")).reminders_sent == [15]


def test_booking_service_race_through_shared_database():
    path = os.path.join(tempfile.mkdtemp(), "svc.sqlite3")

    async def seed():
        s = SQLiteStorage(path)
        await s.save_availability(Availability.from_hours("h", "UTC", {"mon": "09:00-10:00"}))
        await s.close()

    run(seed())

    async def book(i):
        s = SQLiteStorage(path)
        svc = BookingService(s, clock=lambda: utc(2026, 10, 1))
        try:
            await svc.book("h", START, attendee_name=f"P{i}", attendee_email=f"p{i}@x.test")
            return True
        except SlotUnavailable:
            return False
        finally:
            await s.close()

    assert _parallel(6, book).count(True) == 1
