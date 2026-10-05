import threading

from helpers import run, utc
from nodemeet import Booking, RoomConfig
from nodemeet.storage import SyncStorageAdapter


class DictStore:
    """A synchronous backend, like a Django ORM implementation would be."""

    def __init__(self):
        self.rooms, self.bookings, self.threads = {}, {}, set()

    def save_room(self, room):
        self.threads.add(threading.current_thread().name)
        self.rooms[room.id] = room

    def get_room(self, room_id):
        return self.rooms.get(room_id)

    def delete_room(self, room_id):
        self.rooms.pop(room_id, None)

    def list_rooms(self):
        return list(self.rooms.values())

    def save_availability(self, av):
        pass

    def get_availability(self, host_id):
        return None

    def save_booking(self, b):
        self.bookings[b.id] = b

    def get_booking(self, bid):
        return self.bookings.get(bid)

    def list_bookings(self, *, host_id=None, start=None, end=None, status=None):
        return [b for b in self.bookings.values() if host_id in (None, b.host_id)]


def test_sync_storage_runs_off_the_event_loop():
    impl = DictStore()
    s = SyncStorageAdapter(impl)

    async def scenario():
        await s.save_room(RoomConfig(id="r"))
        assert (await s.get_room("r")).id == "r"
        b = Booking("h", utc(2026, 10, 5, 9), utc(2026, 10, 5, 9, 30), "A", "a@x.test")
        assert await s.insert_booking_if_free(b)  # default implementation on top of sync methods
        clash = Booking("h", utc(2026, 10, 5, 9, 15), utc(2026, 10, 5, 9, 45), "B", "b@x.test")
        assert not await s.insert_booking_if_free(clash)

    run(scenario())
    assert impl.threads and "MainThread" not in impl.threads
