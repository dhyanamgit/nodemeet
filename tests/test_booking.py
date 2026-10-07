import os
import tempfile
from datetime import date

import pytest

from helpers import run, utc
from nodemeet import (Availability, BookingService, BookingStatus, MemoryMailer, MemoryStorage,
                      SlotUnavailable, SQLiteStorage)
from nodemeet.exceptions import AvailabilityNotFound, BookingNotFound

NOW = utc(2026, 10, 5, 0, 0)  # Monday 00:00 UTC


def storages():
    tmp = tempfile.mkdtemp()
    return [MemoryStorage(), SQLiteStorage(os.path.join(tmp, "nm.sqlite3"))]


async def make(storage, **kw):
    await storage.setup()
    mailer = MemoryMailer()
    svc = BookingService(storage, mailer=mailer, clock=lambda: NOW,
                         join_url=lambda b, who: f"https://x.test/r/{b.room}?as={who}",
                         manage_url=lambda b: f"https://x.test/manage/{b.id}", **kw)
    await svc.set_availability(Availability.from_hours(
        "h1", "UTC", {"mon-fri": "09:00-11:00"}, host_email="host@x.test", host_name="Hana",
        buffer_after=10, title="Intro call"))
    return svc, mailer


def test_book_cancel_reschedule_all_storages():
    async def scenario(storage):
        svc, mail = await make(storage)
        slots = await svc.find_slots("h1", date(2026, 10, 5), date(2026, 10, 5))
        assert len(slots) == 4
        b = await svc.book("h1", slots[0].start, attendee_name="Ann", attendee_email="ann@x.test",
                           attendee_timezone="Asia/Kolkata", notes="hi")
        assert b.title == "Intro call" and b.status is BookingStatus.CONFIRMED
        # buffer_after=10 removes the 09:30 slot as well
        left = await svc.find_slots("h1", date(2026, 10, 5), date(2026, 10, 5))
        assert [s.start for s in left] == [utc(2026, 10, 5, 10), utc(2026, 10, 5, 10, 30)]
        with pytest.raises(SlotUnavailable):
            await svc.book("h1", slots[0].start, attendee_name="Bob", attendee_email="bob@x.test")
        with pytest.raises(SlotUnavailable):
            await svc.book("h1", utc(2026, 10, 5, 9, 10), attendee_name="Bob", attendee_email="bob@x.test")
        moved = await svc.reschedule(b.id, utc(2026, 10, 6, 10, 30))
        assert moved.sequence == 1 and moved.start == utc(2026, 10, 6, 10, 30)
        assert len(await svc.find_slots("h1", date(2026, 10, 5), date(2026, 10, 5))) == 4
        cancelled = await svc.cancel(b.id, reason="conflict")
        assert cancelled.status is BookingStatus.CANCELLED and cancelled.sequence == 2
        assert (await svc.get(b.id)).cancel_reason == "conflict"
        subjects = [m.subject.split(":")[0] for m in mail.outbox]
        assert subjects == ["Confirmed", "Confirmed", "Rescheduled", "Rescheduled",
                            "Cancelled", "Cancelled"]
        ics = mail.outbox[-1].attachments[0][1]
        assert "METHOD:CANCEL" in ics and "SEQUENCE:2" in ics
        assert mail.outbox[0].to == ["ann@x.test"] and "Asia/Kolkata" in mail.outbox[0].subject
        await storage.close()

    for storage in storages():
        run(scenario(storage))


def test_validation_and_missing():
    async def scenario():
        svc, _ = await make(MemoryStorage())
        with pytest.raises(ValueError):
            await svc.book("h1", utc(2026, 10, 5, 9), attendee_name="A", attendee_email="nope")
        with pytest.raises(AvailabilityNotFound):
            await svc.find_slots("ghost", date(2026, 10, 5), date(2026, 10, 5))
        with pytest.raises(BookingNotFound):
            await svc.cancel("bk_missing")
    run(scenario())


def test_busy_provider_blocks_external_events():
    async def busy(host_id, start, end):
        return [(utc(2026, 10, 5, 9), utc(2026, 10, 5, 10))]

    async def scenario():
        svc, _ = await make(MemoryStorage(), busy_provider=busy)
        slots = await svc.find_slots("h1", date(2026, 10, 5), date(2026, 10, 5))
        # 10:00 is inside the 10-minute buffer after the external 09:00-10:00 event
        assert [s.start for s in slots] == [utc(2026, 10, 5, 10, 30)]
    run(scenario())


def test_hooks_fire_for_bookings():
    events = []

    async def scenario():
        svc, _ = await make(MemoryStorage())
        svc.hooks.on("on_booking_created", lambda b: events.append(("created", b.id)))
        svc.hooks.on("on_booking_rescheduled", lambda b, old: events.append(("moved", old)))
        b = await svc.book("h1", utc(2026, 10, 5, 9), attendee_name="A", attendee_email="a@x.test",
                           notify=False)
        await svc.reschedule(b.id, utc(2026, 10, 5, 10), notify=False)
        assert events == [("created", b.id), ("moved", utc(2026, 10, 5, 9))]
    run(scenario())


def test_concurrent_double_booking_is_prevented():
    import asyncio

    async def scenario():
        svc, _ = await make(MemoryStorage())
        start = utc(2026, 10, 5, 9)
        results = await asyncio.gather(*[
            svc.book("h1", start, attendee_name=f"P{i}", attendee_email=f"p{i}@x.test", notify=False)
            for i in range(5)], return_exceptions=True)
        assert sum(1 for r in results if not isinstance(r, Exception)) == 1
    run(scenario())
