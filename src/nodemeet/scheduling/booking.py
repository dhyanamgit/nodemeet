"""Booking service: book, cancel, reschedule, notify."""
from __future__ import annotations

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..exceptions import AvailabilityNotFound, BookingNotFound, SlotUnavailable
from ..hooks import Hooks
from ..integrations.email import EmailMessage, Mailer
from ..integrations.email_templates import EmailContext, EmailTemplates
from ..integrations.webhooks import WebhookDispatcher
from ..models import Booking, BookingStatus, to_utc, utcnow
from .availability import Availability
from .calendar_links import calendar_links
from .ics import build_ics
from .slots import Interval, Slot, find_slots

if TYPE_CHECKING:
    from ..storage.base import Storage

log = logging.getLogger("nodemeet.booking")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

UrlBuilder = Callable[[Booking, str], str]  # (booking, "host"|"attendee") -> url
BusyProvider = Callable[[str, datetime, datetime], Awaitable[Sequence[Interval]]]


class BookingService:
    """High-level scheduling API. Works with or without the HTTP server.

    ``busy_provider`` lets you feed extra busy times from *your* systems
    (another calendar, shifts table, ...): ``async def f(host_id, start, end)``.
    """

    def __init__(self, storage: Storage, *, mailer: Optional[Mailer] = None,
                 templates: Optional[EmailTemplates] = None,
                 webhooks: Optional[WebhookDispatcher] = None, hooks: Optional[Hooks] = None,
                 join_url: Optional[UrlBuilder] = None,
                 manage_url: Optional[Callable[[Booking], str]] = None,
                 busy_provider: Optional[BusyProvider] = None,
                 organizer_email: str = "", organizer_name: str = "",
                 notify_host: bool = True, clock: Callable[[], datetime] = utcnow) -> None:
        self.storage = storage
        self.mailer = mailer
        self.templates = templates or EmailTemplates()
        self.webhooks = webhooks
        self.hooks = hooks or Hooks()
        self.join_url = join_url
        self.manage_url = manage_url
        self.busy_provider = busy_provider
        self.organizer_email, self.organizer_name = organizer_email, organizer_name
        self.notify_host = notify_host
        self.clock = clock
        # async (event, booking, extra) callbacks: calendar sync, payments, SMS...
        self.listeners: List[Callable[..., Awaitable[None]]] = []
        # extra async (host_id, start, end) -> busy intervals (connected calendars)
        self.busy_sources: List[BusyProvider] = []
        self.payments: Any = None  # nodemeet.integrations.payments.PaymentService
        self.notifier: Any = None  # nodemeet.integrations.sms.Notifier
        # async (tenant_id) -> branding dict; set by NodeMeet so emails match your brand
        self.branding_provider: Optional[Callable[[Optional[str]], Awaitable[Dict[str, Any]]]] = None

    # -- availability ---------------------------------------------------
    async def set_availability(self, availability: Availability) -> Availability:
        await self.storage.save_availability(availability)
        if self.webhooks is not None:
            await self.webhooks.emit("availability.updated", availability.to_dict(), tenant_id=availability.tenant_id)
        return availability

    async def get_availability(self, host_id: str) -> Availability:
        av = await self.storage.get_availability(host_id)
        if av is None:
            raise AvailabilityNotFound(host_id)
        return av

    async def busy_for(self, host_id: str, start: datetime, end: datetime, *,
                       exclude: Optional[str] = None) -> List[Interval]:
        bookings = await self.storage.list_bookings(host_id=host_id, start=start, end=end,
                                                    status=BookingStatus.CONFIRMED)
        busy = [(b.start, b.end) for b in bookings if b.id != exclude]
        if self.busy_provider is not None:
            busy.extend(await self.busy_provider(host_id, start, end))
        for source in self.busy_sources:
            try:
                busy.extend(await source(host_id, start, end))
            except Exception:  # noqa: BLE001 - a calendar outage must not break booking
                log.exception("busy source %r failed", source)
        return busy

    async def find_slots(self, host_id: str, start: date | datetime, end: date | datetime, *,
                         duration_minutes: Optional[int] = None, limit: Optional[int] = None,
                         exclude_booking: Optional[str] = None) -> List[Slot]:
        av = await self.get_availability(host_id)
        lo = start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time())
        hi = end if isinstance(end, datetime) else datetime.combine(end, datetime.max.time())
        pad = timedelta(days=1, minutes=av.buffer_before + av.buffer_after)
        busy = await self.busy_for(host_id, to_utc(lo) - pad, to_utc(hi) + pad,
                                   exclude=exclude_booking)
        return find_slots(av, start, end, busy=busy, duration_minutes=duration_minutes,
                          now=self.clock(), limit=limit)

    async def is_available(self, host_id: str, start: datetime, duration_minutes: int, *,
                           exclude_booking: Optional[str] = None) -> bool:
        start = to_utc(start)
        av = await self.get_availability(host_id)
        local_day = start.astimezone(av.tz).date()
        slots = await self.find_slots(host_id, local_day, local_day,
                                      duration_minutes=duration_minutes,
                                      exclude_booking=exclude_booking)
        end = start + timedelta(minutes=duration_minutes)
        return any(s.start == start and s.end == end for s in slots)

    # -- bookings -------------------------------------------------------
    async def get(self, booking_id: str) -> Booking:
        b = await self.storage.get_booking(booking_id)
        if b is None:
            raise BookingNotFound(booking_id)
        return b

    async def list(self, **filters: Any) -> List[Booking]:
        return await self.storage.list_bookings(**filters)

    async def book(self, host_id: str, start: datetime, *, attendee_name: str,
                   attendee_email: str, duration_minutes: Optional[int] = None,
                   title: Optional[str] = None, notes: str = "",
                   attendee_timezone: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None, notify: bool = True,
                   check_availability: bool = True, tenant_id: Optional[str] = None,
                   attendee_phone: Optional[str] = None) -> Booking:
        if not attendee_name or not _EMAIL_RE.match(attendee_email or ""):
            raise ValueError("attendee_name and a valid attendee_email are required")
        av = await self.get_availability(host_id)
        minutes = duration_minutes or av.duration_minutes
        start = to_utc(start)
        if check_availability and not await self.is_available(host_id, start, minutes):
            raise SlotUnavailable("that time is no longer available")
        booking = Booking(host_id=host_id, start=start,
                          end=start + timedelta(minutes=minutes),
                          attendee_name=attendee_name.strip(),
                          attendee_email=attendee_email.strip(),
                          title=title or av.title, notes=notes,
                          attendee_timezone=attendee_timezone, metadata=dict(metadata or {}),
                          created_at=self.clock(), updated_at=self.clock(),
                          tenant_id=tenant_id or av.tenant_id,
                          attendee_phone=(attendee_phone or "").strip() or None)
        needs_payment = av.price > 0 and self.payments is not None
        if needs_payment:  # hold the slot until paid
            booking.metadata["payment"] = {
                "status": "pending", "amount": av.price, "currency": av.currency,
                "expires_at": (self.clock() + timedelta(minutes=av.payment_hold_minutes)).isoformat()}
        # Atomic in the database: safe with many workers / servers.
        if not await self.storage.insert_booking_if_free(
                booking, buffer_before=av.buffer_before, buffer_after=av.buffer_after):
            raise SlotUnavailable("that time was just booked by someone else")
        if needs_payment:
            try:
                await self.payments.start_checkout(booking, av)
            except Exception:
                booking.status = BookingStatus.CANCELLED
                booking.cancel_reason = "payment provider error"
                await self.storage.save_booking(booking)
                raise
            await self._webhook("booking.pending_payment", booking)
            await self._event("payment.checkout_created", booking, provider=getattr(self.payments.provider, "name", ""),
                              url=booking.payment.get("url"), amount=av.price, currency=av.currency)
            return booking
        await self._confirmed(booking, av, notify)
        return booking

    async def _confirmed(self, booking: Booking, av: Optional[Availability], notify: bool = True) -> None:
        await self.hooks.emit("on_booking_created", booking)
        await self._webhook("booking.created", booking)
        if notify:
            await self._notify("confirmation", booking, av)

    async def mark_paid(self, booking_id: str, *, provider: str, payment_id: str,
                        amount: Optional[int] = None) -> Booking:
        """Called by the payment webhook: confirms the booking and sends the emails."""
        booking = await self.get(booking_id)
        pay = booking.payment
        if pay.get("status") == "paid":
            return booking
        pay.update({"status": "paid", "provider": provider, "payment_id": payment_id,
                    "paid_at": self.clock().isoformat()})
        if amount is not None:
            pay["amount_paid"] = amount
        booking.metadata["payment"] = pay
        if booking.status == BookingStatus.CANCELLED:  # paid after the hold expired
            pay["status"] = "paid_after_expiry"
            booking.metadata["payment"] = pay
            await self.storage.save_booking(booking)
            await self._webhook("booking.payment_late", booking)
            return booking
        booking.updated_at = self.clock()
        await self.storage.save_booking(booking)
        await self._webhook("booking.paid", booking)
        await self._confirmed(booking, await self.storage.get_availability(booking.host_id))
        return booking

    async def release_unpaid(self, now: Optional[datetime] = None) -> List[Booking]:
        """Cancel bookings whose payment hold expired (the reminder loop calls this)."""
        now = to_utc(now) if now else self.clock()
        out = []
        for b in await self.storage.list_bookings(start=now - timedelta(days=1), status=BookingStatus.CONFIRMED):
            exp = b.payment.get("expires_at")
            if b.awaiting_payment and exp and datetime.fromisoformat(exp) <= now:
                cancelled = await self.cancel(b.id, reason="payment not completed", notify=False)
                await self._webhook("booking.payment_expired", cancelled)
                out.append(cancelled)
        return out

    async def cancel(self, booking_id: str, *, reason: Optional[str] = None,
                     notify: bool = True) -> Booking:
        booking = await self.get(booking_id)
        if booking.status == BookingStatus.CANCELLED:
            return booking
        booking.status = BookingStatus.CANCELLED
        booking.cancel_reason = reason
        booking.sequence += 1
        booking.updated_at = self.clock()
        await self.storage.save_booking(booking)
        await self.hooks.emit("on_booking_cancelled", booking)
        await self._webhook("booking.cancelled", booking)
        if notify:
            av = await self.storage.get_availability(booking.host_id)
            await self._notify("cancellation", booking, av, reason=reason or "")
        return booking

    async def reschedule(self, booking_id: str, new_start: datetime, *,
                         duration_minutes: Optional[int] = None, notify: bool = True,
                         check_availability: bool = True) -> Booking:
        booking = await self.get(booking_id)
        if booking.status != BookingStatus.CONFIRMED:
            raise SlotUnavailable("cannot reschedule a cancelled booking")
        minutes = duration_minutes or booking.duration_minutes
        new_start = to_utc(new_start)
        av = await self.get_availability(booking.host_id)
        if check_availability and not await self.is_available(
                booking.host_id, new_start, minutes, exclude_booking=booking.id):
            raise SlotUnavailable("that time is not available")
        old_start = booking.start
        booking.start = new_start
        booking.end = new_start + timedelta(minutes=minutes)
        booking.sequence += 1
        booking.reminders_sent = []
        booking.updated_at = self.clock()
        if not await self.storage.insert_booking_if_free(
                booking, buffer_before=av.buffer_before, buffer_after=av.buffer_after):
            raise SlotUnavailable("that time was just booked by someone else")
        await self.hooks.emit("on_booking_rescheduled", booking, old_start)
        await self._webhook("booking.rescheduled", booking, old_start=old_start.isoformat())
        if notify:
            await self._notify("rescheduled", booking, av, old_start=old_start)
        return booking

    async def send_reminder(self, booking: Booking, minutes_before: int) -> None:
        av = await self.storage.get_availability(booking.host_id)
        await self.hooks.emit("on_reminder", booking, minutes_before)
        await self._webhook("booking.reminder", booking, minutes_before=minutes_before)
        await self._notify("reminder", booking, av, minutes_before=minutes_before)

    # -- helpers ----------------------------------------------------------
    def url_for(self, booking: Booking, who: str = "attendee") -> str:
        return self.join_url(booking, who) if self.join_url else ""

    def ics(self, booking: Booking, who: str = "attendee") -> str:
        return build_ics(booking, organizer_email=self.organizer_email,
                         organizer_name=self.organizer_name, url=self.url_for(booking, who) or None)

    def calendar_links(self, booking: Booking, who: str = "attendee") -> Dict[str, str]:
        return calendar_links(booking, join_url=self.url_for(booking, who) or None)

    async def _event(self, event: str, booking: Booking, **data: Any) -> None:
        """Lightweight event (no listeners): email/sms outcomes and similar."""
        if self.webhooks is not None:
            try:
                await self.webhooks.emit(event, {"booking_id": booking.id, "host_id": booking.host_id, **data},
                                         tenant_id=booking.tenant_id)
                if event.endswith(".failed"):
                    await self.webhooks.emit("integration.error", {"source": event.split(".")[0], "event": event,
                                                                   "booking_id": booking.id, **data},
                                             tenant_id=booking.tenant_id)
            except Exception:  # noqa: BLE001
                log.exception("event %s failed", event)

    async def check_attendance(self, now: Optional[datetime] = None, grace_minutes: int = 15) -> List[Booking]:
        """Emit ``booking.no_show`` for meetings that ended without both sides joining."""
        now = to_utc(now) if now else self.clock()
        out = []
        for b in await self.storage.list_bookings(start=now - timedelta(days=2), status=BookingStatus.CONFIRMED):
            if b.end + timedelta(minutes=grace_minutes) > now or b.metadata.get("attendance_checked"):
                continue
            if b.awaiting_payment:
                continue
            seen = b.metadata.get("attendance") or {}
            missing = [who for who in ("host", "attendee") if who not in seen]
            b.metadata["attendance_checked"] = True
            await self.storage.save_booking(b)
            if missing:
                await self._webhook("booking.no_show", b, missing=missing)
                out.append(b)
        return out

    async def _webhook(self, event: str, booking: Booking, **extra: Any) -> None:
        if self.webhooks is not None:
            data = {"booking": booking.to_dict(include_secrets=False), **extra}
            await self.webhooks.emit(event, data, tenant_id=booking.tenant_id)
        for listener in list(self.listeners):
            try:
                await listener(event, booking, extra)
            except Exception:  # noqa: BLE001 - integrations must not break bookings
                log.exception("booking listener %r failed for %s", listener, event)

    def _recipients(self, booking: Booking, av: Optional[Availability]) -> List[Tuple[str, str, str]]:
        """(email, who, timezone) tuples."""
        out = [(booking.attendee_email, "attendee",
                booking.attendee_timezone or (av.timezone if av else "UTC"))]
        if self.notify_host and av is not None and av.host_email:
            out.append((av.host_email, "host", av.timezone))
        return out

    async def _notify(self, kind: str, booking: Booking, av: Optional[Availability],
                      **ctx_kwargs: Any) -> None:
        if self.notifier is not None and booking.attendee_phone:
            try:
                await self.notifier.booking_message(kind, booking, av, self.url_for(booking, "attendee"),
                                                    **ctx_kwargs)
                await self._event("sms.sent", booking, kind=kind, to=booking.attendee_phone,
                                  channels=list(getattr(self.notifier, "channels", []) or []))
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to send %s text message for %s", kind, booking.id)
                await self._event("sms.failed", booking, kind=kind, to=booking.attendee_phone, error=str(exc)[:300])
        if self.mailer is None:
            return
        templates = self.templates
        if self.branding_provider is not None:
            try:
                templates = templates.with_branding(await self.branding_provider(booking.tenant_id))
            except Exception:  # noqa: BLE001
                log.exception("branding lookup failed; using default email look")
        render = getattr(templates, kind)
        for email, who, tz in self._recipients(booking, av):
            ctx = EmailContext(
                join_url=self.url_for(booking, who),
                manage_url=self.manage_url(booking) if (self.manage_url and who == "attendee") else "",
                host_name=(av.host_name if av else "") if who == "attendee" else booking.attendee_name,
                timezone=tz, calendar_links=self.calendar_links(booking, who), **ctx_kwargs)
            subject, text, html_body = render(booking, ctx)
            msg = EmailMessage(to=[email], subject=subject, text=text, html=html_body)
            if kind != "reminder":
                method = "CANCEL" if kind == "cancellation" else "REQUEST"
                msg.attachments.append(("invite.ics", self.ics(booking, who), "text/calendar"))
                msg.calendar_method = method
            try:
                await self.mailer.send(msg)
                await self._event("email.sent", booking, kind=kind, to=email, who=who, subject=subject)
            except Exception as exc:  # noqa: BLE001 - email failure must not undo a booking
                log.exception("failed to send %s email for %s", kind, booking.id)
                await self._event("email.failed", booking, kind=kind, to=email, who=who, error=str(exc)[:300])
