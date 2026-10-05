"""Background reminder loop.

Runs inside the server (or any asyncio app). For multi-process deployments,
run it in exactly one worker, or call :meth:`ReminderScheduler.run_once`
from your own cron/job queue.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from ..models import Booking, BookingStatus, to_utc

if TYPE_CHECKING:
    from .booking import BookingService

log = logging.getLogger("nodemeet.reminders")


class ReminderScheduler:
    def __init__(self, service: "BookingService", offsets_minutes: Iterable[int] = (1440, 15),
                 interval: float = 60.0) -> None:
        self.service = service
        self.offsets = sorted({int(m) for m in offsets_minutes if int(m) > 0}, reverse=True)
        self.interval = interval
        self._task: Optional["asyncio.Task[None]"] = None

    def due_offsets(self, booking: Booking, now: datetime) -> Tuple[Optional[int], List[int]]:
        """Return (offset to send or None, offsets to mark as handled)."""
        if booking.start <= now:
            return None, []
        due = [m for m in self.offsets if m not in booking.reminders_sent
               and now >= booking.start - timedelta(minutes=m)]
        if not due:
            return None, []
        # Only remind for offsets that existed when they became due, e.g. no
        # "starts in 1 day" mail for a meeting booked 2 hours ahead.
        eligible = [m for m in due if booking.created_at <= booking.start - timedelta(minutes=m)]
        return (min(eligible) if eligible else None), due

    async def run_once(self, now: Optional[datetime] = None) -> List[Tuple[Booking, int]]:
        if not self.offsets:
            return []
        now = to_utc(now) if now else self.service.clock()
        try:
            await self.service.release_unpaid(now)
            await self.service.check_attendance(now)
        except Exception:  # noqa: BLE001
            log.exception("releasing unpaid bookings failed")
        horizon = now + timedelta(minutes=max(self.offsets))
        sent: List[Tuple[Booking, int]] = []
        bookings = await self.service.storage.list_bookings(
            start=now, end=horizon, status=BookingStatus.CONFIRMED)
        for booking in bookings:
            if booking.awaiting_payment:
                continue
            offset, handled = self.due_offsets(booking, now)
            if not handled:
                continue
            # Claims are atomic in the storage, so with several workers each
            # reminder is still sent exactly once.
            claimed = [m for m in handled
                       if await self.service.storage.mark_reminder_sent(booking.id, m)]
            if offset is not None and offset in claimed:
                try:
                    await self.service.send_reminder(booking, offset)
                    sent.append((booking, offset))
                except Exception:  # noqa: BLE001
                    log.exception("reminder for %s failed", booking.id)
        return sent

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reminder loop iteration failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
