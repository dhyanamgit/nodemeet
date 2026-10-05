"""Text-message notifications: SMS and WhatsApp for confirmations and reminders.

    from nodemeet.integrations.sms import TwilioNotifier, WhatsAppCloudNotifier, WebhookNotifier
    meet.bookings.notifier = TwilioNotifier(SID, TOKEN, sms_from="+15550001111", whatsapp_from="+14155238886")

Bookings need ``attendee_phone`` (E.164, e.g. ``+919812345678``). Turn on the phone
field in the booking widget with ``Availability(collect_phone=True)``.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .http_client import HTTPClient

if TYPE_CHECKING:
    from ..models import Booking
    from ..scheduling.availability import Availability

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
TEXTS = {
    "confirmation": "Booked: {title} on {when}. Join: {url}",
    "reminder": "Reminder: {title} starts {when}. Join: {url}",
    "rescheduled": "Moved: {title} is now {when}. Join: {url}",
    "cancellation": "Cancelled: {title} on {when}.",
}


class Notifier:
    """Base class: implement :meth:`send`. ``channels`` picks sms and/or whatsapp."""

    channels: List[str] = ["sms"]
    texts: Dict[str, str] = TEXTS

    async def send(self, to: str, text: str, channel: str = "sms") -> None:
        raise NotImplementedError

    def render(self, kind: str, b: "Booking", av: Optional["Availability"], url: str) -> str:
        tz = ZoneInfo(b.attendee_timezone or (av.timezone if av else "UTC"))
        when = b.start.astimezone(tz).strftime("%a %d %b, %H:%M %Z")
        return self.texts.get(kind, "{title} {when}").format(title=b.title, when=when, url=url)

    async def booking_message(self, kind: str, b: "Booking", av: Optional["Availability"], url: str,
                              **_: Any) -> None:
        phone = (b.attendee_phone or "").replace(" ", "")
        if not E164.match(phone) or kind not in self.texts:
            return
        text = self.render(kind, b, av, url)
        for channel in self.channels:
            await self.send(phone, text, channel)


class TwilioNotifier(Notifier):
    def __init__(self, account_sid: str, auth_token: str, *, sms_from: Optional[str] = None,
                 whatsapp_from: Optional[str] = None, messaging_service_sid: Optional[str] = None,
                 http: Optional[HTTPClient] = None) -> None:
        self.sid, self.token = account_sid, auth_token
        self.sms_from, self.whatsapp_from, self.service = sms_from, whatsapp_from, messaging_service_sid
        self.channels = [c for c, ok in (("sms", sms_from or messaging_service_sid), ("whatsapp", whatsapp_from)) if ok]
        self.http = http or HTTPClient()
        self.status_callback: Any = None  # str or () -> str: delivery receipts URL

    async def send(self, to: str, text: str, channel: str = "sms") -> None:
        form: Dict[str, Any] = {"To": f"whatsapp:{to}" if channel == "whatsapp" else to, "Body": text[:1600]}
        if channel == "whatsapp":
            form["From"] = f"whatsapp:{self.whatsapp_from}"
        elif self.service:
            form["MessagingServiceSid"] = self.service
        else:
            form["From"] = self.sms_from
        cb = self.status_callback() if callable(self.status_callback) else self.status_callback
        if cb:
            form["StatusCallback"] = cb
        await self.http.request("POST", f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                                basic=(self.sid, self.token), form=form, expect_ok=True, what="twilio")


class WhatsAppCloudNotifier(Notifier):
    """Meta's WhatsApp Cloud API. Business-initiated messages must use an approved
    template: pass ``template="booking_update"`` with one body parameter ({{1}} = text)."""

    channels = ["whatsapp"]

    def __init__(self, phone_number_id: str, access_token: str, *, template: Optional[str] = None,
                 language: str = "en", api_version: str = "v20.0", http: Optional[HTTPClient] = None) -> None:
        self.phone_id, self.token, self.template, self.language = phone_number_id, access_token, template, language
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self.http = http or HTTPClient()

    async def send(self, to: str, text: str, channel: str = "whatsapp") -> None:
        body: Dict[str, Any] = {"messaging_product": "whatsapp", "to": to.lstrip("+")}
        if self.template:
            body.update(type="template", template={"name": self.template, "language": {"code": self.language},
                                                   "components": [{"type": "body", "parameters": [
                                                       {"type": "text", "text": text[:1000]}]}]})
        else:
            body.update(type="text", text={"body": text[:4096]})
        await self.http.request("POST", self.url, bearer=self.token, json_body=body, expect_ok=True, what="whatsapp")


class WebhookNotifier(Notifier):
    """POST ``{"to", "text", "channel"}`` to your own endpoint (MSG91, Vonage, Plivo, SNS...)."""

    def __init__(self, url: str, *, channels: Optional[List[str]] = None, headers: Optional[Dict[str, str]] = None,
                 http: Optional[HTTPClient] = None) -> None:
        self.url, self.headers = url, headers or {}
        self.channels = channels or ["sms"]
        self.http = http or HTTPClient()

    async def send(self, to: str, text: str, channel: str = "sms") -> None:
        await self.http.request("POST", self.url, json_body={"to": to, "text": text, "channel": channel},
                                headers=self.headers, expect_ok=True, what="sms webhook")


class MemoryNotifier(Notifier):
    """Collects messages (tests and demos)."""

    def __init__(self, channels: Optional[List[str]] = None) -> None:
        self.channels = channels or ["sms"]
        self.outbox: List[Dict[str, str]] = []

    async def send(self, to: str, text: str, channel: str = "sms") -> None:
        self.outbox.append({"to": to, "text": text, "channel": channel})
