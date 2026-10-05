"""Plug nodemeet into YOUR database by implementing the Storage interface.

This sketch uses asyncpg (Postgres); any async driver/ORM works the same way.
"""
import json

from nodemeet import Availability, Booking, RoomConfig, Storage


class PostgresStorage(Storage):
    def __init__(self, pool):
        self.pool = pool  # asyncpg.create_pool(...)

    async def save_room(self, room):
        await self.pool.execute("INSERT INTO rooms(id,data) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET data=$2",
                                room.id, json.dumps(room.to_dict()))

    async def get_room(self, room_id):
        row = await self.pool.fetchrow("SELECT data FROM rooms WHERE id=$1", room_id)
        return RoomConfig.from_dict(json.loads(row["data"])) if row else None

    async def delete_room(self, room_id):
        await self.pool.execute("DELETE FROM rooms WHERE id=$1", room_id)

    async def list_rooms(self):
        return [RoomConfig.from_dict(json.loads(r["data"])) for r in await self.pool.fetch("SELECT data FROM rooms")]

    async def save_availability(self, av):
        await self.pool.execute("INSERT INTO availability(host_id,data) VALUES($1,$2) ON CONFLICT(host_id) DO UPDATE SET data=$2",
                                av.host_id, json.dumps(av.to_dict()))

    async def get_availability(self, host_id):
        row = await self.pool.fetchrow("SELECT data FROM availability WHERE host_id=$1", host_id)
        return Availability.from_dict(json.loads(row["data"])) if row else None

    async def save_booking(self, b):
        await self.pool.execute(
            "INSERT INTO bookings(id,host_id,start_at,end_at,status,data) VALUES($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT(id) DO UPDATE SET start_at=$3,end_at=$4,status=$5,data=$6",
            b.id, b.host_id, b.start, b.end, b.status.value, json.dumps(b.to_dict()))

    async def get_booking(self, booking_id):
        row = await self.pool.fetchrow("SELECT data FROM bookings WHERE id=$1", booking_id)
        return Booking.from_dict(json.loads(row["data"])) if row else None

    async def list_bookings(self, *, host_id=None, start=None, end=None, status=None):
        rows = await self.pool.fetch(
            "SELECT data FROM bookings WHERE ($1::text IS NULL OR host_id=$1) AND ($2::timestamptz IS NULL OR end_at>$2) "
            "AND ($3::timestamptz IS NULL OR start_at<$3) AND ($4::text IS NULL OR status=$4) ORDER BY start_at",
            host_id, start, end, status.value if status else None)
        return [Booking.from_dict(json.loads(r["data"])) for r in rows]

# meet = NodeMeet(secret, storage=PostgresStorage(pool))
