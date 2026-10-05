from datetime import timedelta

from helpers import run, utc
from nodemeet import (Availability, BookingService, MemoryMailer, MemoryStorage,
                      ReminderScheduler)


def test_reminders_sent_once_per_offset():
    now = [utc(2026, 10, 1, 0, 0)]

    async def scenario():
        mail = MemoryMailer()
        svc = BookingService(MemoryStorage(), mailer=mail, clock=lambda: now[0])
        await svc.set_availability(Availability.from_hours("h", "UTC", {"mon-fri": "09:00-10:00"}))
        b = await svc.book("h", utc(2026, 10, 5, 9), attendee_name="A", attendee_email="a@x.test",
                           notify=False)
        r = ReminderScheduler(svc, offsets_minutes=(1440, 15))
        assert await r.run_once(utc(2026, 10, 3, 9)) == []  # too early
        sent = await r.run_once(utc(2026, 10, 4, 9, 1))
        assert [m for _, m in sent] == [1440]
        assert await r.run_once(utc(2026, 10, 4, 9, 2)) == []  # not twice
        sent = await r.run_once(utc(2026, 10, 5, 8, 50))
        assert [m for _, m in sent] == [15]
        assert (await svc.get(b.id)).reminders_sent == [15, 1440]
        assert [m.subject for m in mail.outbox] == ["Reminder: Meeting starts in 1 day",
                                                   "Reminder: Meeting starts in 15 minutes"]

    run(scenario())


def test_late_booking_skips_stale_reminders():
    now = [utc(2026, 10, 5, 7, 0)]

    async def scenario():
        svc = BookingService(MemoryStorage(), mailer=MemoryMailer(), clock=lambda: now[0])
        await svc.set_availability(Availability.from_hours("h", "UTC", {"mon": "09:00-10:00"}))
        await svc.book("h", utc(2026, 10, 5, 9), attendee_name="A", attendee_email="a@x.test",
                       notify=False)
        r = ReminderScheduler(svc, offsets_minutes=(1440, 15))
        assert await r.run_once(utc(2026, 10, 5, 7, 1)) == []  # 1-day reminder is stale
        sent = await r.run_once(utc(2026, 10, 5, 8, 46))
        assert [m for _, m in sent] == [15]

    run(scenario())


def test_cancelled_bookings_get_no_reminders():
    async def scenario():
        clock = lambda: utc(2026, 10, 1)  # noqa: E731
        svc = BookingService(MemoryStorage(), clock=clock)
        await svc.set_availability(Availability.from_hours("h", "UTC", {"mon": "09:00-10:00"}))
        b = await svc.book("h", utc(2026, 10, 5, 9), attendee_name="A", attendee_email="a@x.test")
        await svc.cancel(b.id)
        r = ReminderScheduler(svc)
        assert await r.run_once(b.start - timedelta(minutes=5)) == []

    run(scenario())
