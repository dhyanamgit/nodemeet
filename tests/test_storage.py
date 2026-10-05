import os
import tempfile

from helpers import run, utc
from nodemeet import Availability, Booking, BookingStatus, ChatMessage, MemoryStorage, RoomConfig, SQLiteStorage


def test_storage_contract():
    async def check(s):
        await s.setup()
        await s.save_room(RoomConfig(id="r1", name="One", mode="webinar"))
        assert (await s.get_room("r1")).mode == "webinar"
        assert [r.id for r in await s.list_rooms()] == ["r1"]
        av = Availability.from_hours("h", "Europe/Berlin", {"mon": "09:00-10:00"})
        await s.save_availability(av)
        assert await s.get_availability("h") == av
        b1 = Booking("h", utc(2026, 10, 5, 9), utc(2026, 10, 5, 9, 30), "A", "a@x.test")
        b2 = Booking("h", utc(2026, 10, 6, 9), utc(2026, 10, 6, 9, 30), "B", "b@x.test",
                     status=BookingStatus.CANCELLED)
        b3 = Booking("other", utc(2026, 10, 5, 9), utc(2026, 10, 5, 9, 30), "C", "c@x.test")
        for b in (b2, b1, b3):
            await s.save_booking(b)
        assert await s.get_booking(b1.id) == b1
        assert [b.id for b in await s.list_bookings(host_id="h")] == [b1.id, b2.id]
        got = await s.list_bookings(host_id="h", status=BookingStatus.CONFIRMED)
        assert [b.id for b in got] == [b1.id]
        window = await s.list_bookings(start=utc(2026, 10, 5, 9, 29), end=utc(2026, 10, 6))
        assert {b.id for b in window} == {b1.id, b3.id}
        assert await s.list_bookings(start=utc(2026, 10, 5, 9, 30), end=utc(2026, 10, 6)) == []
        for i in range(5):
            await s.save_chat(ChatMessage(room="r1", peer_id="p", user_id="u", name="U", text=str(i)))
        assert [m.text for m in await s.list_chat("r1", limit=3)] == ["2", "3", "4"]
        await s.delete_room("r1")
        assert await s.get_room("r1") is None and await s.list_chat("r1") == []
        await s.close()

    run(check(MemoryStorage()))
    run(check(SQLiteStorage(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))))


def test_sqlite_persists_across_instances():
    path = os.path.join(tempfile.mkdtemp(), "p.sqlite3")

    async def scenario():
        s = SQLiteStorage(path)
        await s.save_room(RoomConfig(id="keep"))
        await s.close()
        s2 = SQLiteStorage(path)
        assert (await s2.get_room("keep")) is not None
        await s2.close()

    run(scenario())
