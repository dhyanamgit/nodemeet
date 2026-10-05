"""Inbound webhooks: other apps -> nodemeet.

Everything lands under ``/api/inbound/<source>``, is signature-checked, normalised and
re-emitted as nodemeet events (so your own webhooks / Slack / CRM / push see it too):

=========================  ======================================================  =========================
source                     what it understands                                     secret (add_* option)
=========================  ======================================================  =========================
``actions``  (POST)        do things from Zapier / Make / n8n / your backend:      API key, or HMAC with
                           room.create/update/broadcast, link.create,              ``NodeMeet(inbound_secret=)``
                           booking.create/cancel/reschedule, push.notify,
                           chat.post, sms.send, email.send, event.emit
``zoom``                   meeting started/ended, participant joined/left,         ``webhook_secret``
                           recording completed, URL validation
``webex``                  meeting started/ended, participant joined/left          ``webhook_secret``
``microsoft``              Graph change notifications (Outlook calendar, Teams     ``client_state``
                           call records) incl. validation handshake
``google-calendar``        Calendar push channels -> busy times refreshed          ``channel_token``
``twilio``                 SMS / WhatsApp delivery receipts + replies              Twilio auth token
``whatsapp``               WhatsApp Cloud messages + statuses (+ GET verify)       ``app_secret``, ``verify_token``
``slack``                  ``/meet`` slash command, Events API url_verification    ``signing_secret``
``telegram``               ``/meet`` bot commands                                  ``secret_token``
``discord``                ``/meet`` slash command (interactions, Ed25519)         ``public_key``
``hubspot``                contact changes / deletions (v3 signatures)             ``client_secret``
``pipedrive``              person / deal / activity changes (basic auth)           ``user``, ``password``
``salesforce``             Outbound Messages (SOAP) for Contacts / Leads / ...     ``org_id``
``zoho``                   workflow webhooks                                       ``token``
=========================  ======================================================  =========================

Texting back works too: attendees can reply **YES** (confirm), **CANCEL** or **RESCHEDULE**.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import escape
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode

from ._http import HTTPError, Request, Response, Router, json_response, text_response
from .integrations.webhooks import verify_signature

if TYPE_CHECKING:
    from .server import NodeMeet

log = logging.getLogger("nodemeet.inbound")
ACTION_SCOPES = {"room": "rooms:write", "link": "tokens:create", "booking": "bookings:write"}


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a).encode(), str(b).encode())


class InboundService:
    def __init__(self, meet: "NodeMeet") -> None:
        self.meet = meet
        self.secrets: Dict[str, Dict[str, Any]] = {}
        self.sms_keywords = True
        self.bot_allow: Dict[str, List[str]] = {}

    def configure(self, source: str, **secrets: Any) -> None:
        self.secrets.setdefault(source, {}).update({k: v for k, v in secrets.items() if v is not None})

    def _secret(self, source: str, key: str) -> Any:
        val = (self.secrets.get(source) or {}).get(key)
        if not val:
            raise HTTPError(404, "not_configured", f"inbound {source} webhooks are not set up ({key} missing)")
        return val

    def register(self, r: Router) -> None:
        r.add("POST", "/api/inbound/actions", self.actions)
        for src in ("zoom", "webex", "microsoft", "google-calendar", "twilio", "whatsapp", "slack", "telegram",
                    "discord", "hubspot", "pipedrive", "salesforce", "zoho"):
            r.add("POST", f"/api/inbound/{src}", getattr(self, src.replace("-", "_")))
        r.add("GET", "/api/inbound/whatsapp", self.whatsapp_verify)
        r.add("GET", "/api/inbound/microsoft", self.microsoft)

    async def _emit(self, event: str, data: Dict[str, Any], source: str, tenant: Optional[str] = None) -> None:
        await self.meet.emit_webhook(event, {"source": source, **data}, tenant_id=tenant)

    async def _reject(self, source: str, why: str) -> None:
        await self.meet.emit_webhook("inbound.rejected", {"source": source, "reason": why})
        raise HTTPError(401, "bad_signature", f"{source}: {why}")

    async def _booking_by_conf(self, provider: str, conf_id: Any) -> Any:
        ref = await self.meet.storage.get_record("conf_ref", f"{provider}|{conf_id}")
        return await self.meet.storage.get_booking(ref["booking_id"]) if ref else None

    # -- generic actions (Zapier / Make / n8n / your backend) -------------------------------------------
    async def actions(self, req: Request) -> Response:
        secret = (self.secrets.get("actions") or {}).get("secret")
        sig = req.header("x-nodemeet-signature")
        tenant = None
        if sig and secret:
            if not verify_signature(secret, req.body, sig):
                await self._reject("actions", "signature mismatch")
        body = req.json() or {}
        action = str(body.get("action", ""))
        if not (sig and secret):
            p = await self.meet.api.require(req, ACTION_SCOPES.get(action.split(".")[0], "webhooks:manage"))
            tenant = None if p.is_master else p.tenant_id
        data = dict(body.get("data") or {})
        try:
            result = await self._do(action, data, tenant)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPError(400, "bad_action", str(exc)) from None
        await self._emit("inbound.received", {"action": action}, "actions", tenant)
        return json_response({"ok": True, "action": action, "result": result})

    async def _do(self, action: str, d: Dict[str, Any], tenant: Optional[str]) -> Any:
        m = self.meet
        if action in ("room.create", "room.update"):
            from .easy import room_settings
            rid = d.pop("room_id", None) or d.pop("id", None) or m.room().id
            preset = d.pop("preset", None)
            if tenant:
                d["tenant_id"] = tenant
            cfg = await m._apply_room(rid, room_settings(preset, **d))
            return cfg.to_dict()
        if action == "room.broadcast":
            live = m.rooms.get(d["room_id"])
            if live is None:
                return {"delivered": 0}
            await live.broadcast({"type": "announcement", "by": d.get("by", "System"), "text": str(d["text"])[:1000]})
            return {"delivered": len(live.participants)}
        if action == "link.create":
            return {"url": m.link(d["room_id"], d.get("name"), d.get("role", "participant"), user_id=d.get("user_id"),
                                  email=d.get("email"), skip_waiting_room=bool(d.get("skip_waiting_room")),
                                  ttl=d.get("ttl"), tenant=tenant, can=d.get("can", ()), cannot=d.get("cannot", ()))}
        if action == "booking.create":
            b = await m.book(d["host_id"], d["start"], name=d["name"], email=d["email"], notes=d.get("notes", ""),
                             attendee_phone=d.get("phone"), metadata=d.get("metadata"))
            return b.to_dict(include_secrets=False)
        if action == "booking.cancel":
            return (await m.cancel(d["booking_id"], d.get("reason"))).to_dict(include_secrets=False)
        if action == "booking.reschedule":
            return (await m.reschedule(d["booking_id"], d["start"])).to_dict(include_secrets=False)
        if action == "push.notify":
            if m.push is None:
                raise ValueError("push is not set up (meet.add_push)")
            return {"devices": await m.push.notify(d["user_id"], d["title"], d.get("body", ""), url=d.get("url"))}
        if action == "chat.post":
            from .integrations.chat import ChatService
            sent = 0
            for tap in m.webhooks.taps:
                svc = getattr(tap, "__self__", None)
                if isinstance(svc, ChatService):
                    await svc.channel.send({"title": d.get("title", ""), "text": d.get("text", ""), "url": d.get("url"),
                                            "color": d.get("color", "#6366f1"), "fields": d.get("fields") or {}})
                    sent += 1
            return {"channels": sent}
        if action == "sms.send":
            if m.bookings.notifier is None:
                raise ValueError("SMS is not set up (meet.add_sms)")
            await m.bookings.notifier.send(d["to"], d["text"], d.get("channel", "sms"))
            return {"sent": True}
        if action == "email.send":
            if m.bookings.mailer is None:
                raise ValueError("email is not set up (meet.add_email)")
            from .integrations.email import EmailMessage
            to = d["to"] if isinstance(d["to"], list) else [d["to"]]
            await m.bookings.mailer.send(EmailMessage(to=to, subject=d["subject"], text=d.get("text", ""), html=d.get("html")))
            return {"sent": len(to)}
        if action == "event.emit":
            name = re.sub(r"[^a-z0-9_.-]", "_", str(d["name"]).lower())[:64]
            await m.emit_webhook(f"custom.{name}", dict(d.get("data") or {}), tenant_id=tenant)
            return {"event": f"custom.{name}"}
        from .easy import did_you_mean
        raise ValueError(f"unknown action {action!r}." + did_you_mean(action, [
            "room.create", "room.update", "room.broadcast", "link.create", "booking.create", "booking.cancel",
            "booking.reschedule", "push.notify", "chat.post", "sms.send", "email.send", "event.emit"]))

    # -- conferencing ----------------------------------------------------------------------------------
    async def zoom(self, req: Request) -> Response:
        secret = self._secret("zoom", "webhook_secret")
        body = req.json() or {}
        if body.get("event") == "endpoint.url_validation":
            plain = body["payload"]["plainToken"]
            return json_response({"plainToken": plain, "encryptedToken":
                                  hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()})
        ts = req.header("x-zm-request-timestamp")
        expected = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + req.body, hashlib.sha256).hexdigest()
        if not ts or not _eq(expected, req.header("x-zm-signature")) or abs(time.time() - int(ts)) > 300:
            await self._reject("zoom", "signature mismatch")
        obj = (body.get("payload") or {}).get("object") or {}
        mapping = {"meeting.started": "conference.started", "meeting.ended": "conference.ended",
                   "meeting.participant_joined": "conference.participant_joined",
                   "meeting.participant_left": "conference.participant_left",
                   "recording.completed": "conference.recording_ready"}
        event = mapping.get(body.get("event", ""))
        if event:
            booking = await self._booking_by_conf("zoom", obj.get("id"))
            person = obj.get("participant") or {}
            await self._emit(event, {"provider": "zoom", "meeting_id": str(obj.get("id")), "topic": obj.get("topic"),
                                     "participant": {"name": person.get("user_name"), "email": person.get("email")} if person else None,
                                     "recording_files": [{"type": f.get("file_type"), "url": f.get("download_url")}
                                                         for f in obj.get("recording_files", [])] or None,
                                     "booking_id": booking.id if booking else None, "raw_event": body.get("event")},
                             "zoom", booking.tenant_id if booking else None)
        return json_response({"ok": True})

    async def webex(self, req: Request) -> Response:
        secret = self._secret("webex", "webhook_secret")
        expected = hmac.new(secret.encode(), req.body, hashlib.sha1).hexdigest()
        if not _eq(expected, req.header("x-spark-signature")):
            await self._reject("webex", "signature mismatch")
        body = req.json() or {}
        res, ev, data = body.get("resource"), body.get("event"), body.get("data") or {}
        event = {("meetings", "started"): "conference.started", ("meetings", "ended"): "conference.ended",
                 ("meetingParticipants", "joined"): "conference.participant_joined",
                 ("meetingParticipants", "left"): "conference.participant_left",
                 ("recordings", "created"): "conference.recording_ready"}.get((res, ev))
        if event:
            mid = data.get("meetingSeriesId") or data.get("meetingId") or data.get("id")
            booking = await self._booking_by_conf("webex", mid) or await self._booking_by_conf("webex", data.get("id"))
            await self._emit(event, {"provider": "webex", "meeting_id": mid, "participant": {
                "name": data.get("displayName"), "email": data.get("email")} if res == "meetingParticipants" else None,
                "booking_id": booking.id if booking else None, "raw_event": f"{res}.{ev}"}, "webex",
                booking.tenant_id if booking else None)
        return json_response({"ok": True})

    async def microsoft(self, req: Request) -> Response:
        token = req.query.get("validationToken")
        if token:  # subscription handshake: echo within 10 seconds, text/plain
            return text_response(token)
        state = self._secret("microsoft", "client_state")
        body = req.json() or {}
        for n in body.get("value", []):
            if not _eq(n.get("clientState", ""), state):
                await self._reject("microsoft", "clientState mismatch")
            res = str(n.get("resource", ""))
            if "callRecords" in res:
                await self._emit("conference.ended", {"provider": "teams", "resource": res,
                                                      "change": n.get("changeType")}, "microsoft")
            elif "events" in res.lower():
                host = (await self.meet.storage.get_record("calendar_watch", n.get("subscriptionId", ""))) or {}
                await self._emit("calendar.changed", {"provider": "outlook", "host_id": host.get("host_id"),
                                                      "change": n.get("changeType"), "resource": res}, "microsoft")
        self._refresh_calendars()
        return Response(status=202)

    async def google_calendar(self, req: Request) -> Response:
        if not _eq(req.header("x-goog-channel-token"), self._secret("google-calendar", "channel_token")):
            await self._reject("google-calendar", "channel token mismatch")
        state = req.header("x-goog-resource-state")
        if state != "sync":
            watch = await self.meet.storage.get_record("calendar_watch", req.header("x-goog-channel-id")) or {}
            self._refresh_calendars()
            await self._emit("calendar.changed", {"provider": "google", "host_id": watch.get("host_id"), "state": state,
                                                  "channel_id": req.header("x-goog-channel-id")}, "google")
        return Response(status=200)

    def _refresh_calendars(self) -> None:
        cal = getattr(self.meet, "calendars", None)
        if cal is not None:
            cal._cache.clear()

    # -- texting -------------------------------------------------------------------------------------
    async def twilio(self, req: Request) -> Response:
        notifier = self.meet.bookings.notifier
        token = getattr(notifier, "token", None) or (self.secrets.get("twilio") or {}).get("auth_token")
        if not token:
            raise HTTPError(404, "not_configured", "Twilio is not set up (meet.add_sms('twilio'))")
        params = dict(parse_qsl(req.body.decode()))
        url = self.meet.base_url + req.path + ("?" + urlencode(req.query) if req.query else "")
        mac = base64.b64encode(hmac.new(token.encode(), (url + "".join(k + params[k] for k in sorted(params))).encode(),
                                        hashlib.sha1).digest()).decode()
        if not _eq(mac, req.header("x-twilio-signature")):
            await self._reject("twilio", "signature mismatch")
        frm = params.get("From", "")
        channel = "whatsapp" if frm.startswith("whatsapp:") else "sms"
        phone = frm.split(":", 1)[-1]
        if "MessageStatus" in params and "Body" not in params:
            status = params["MessageStatus"]
            if status in ("delivered", "read"):
                await self._emit("sms.delivered", {"sid": params.get("MessageSid"), "to": params.get("To", "").split(":")[-1],
                                                   "status": status, "channel": channel}, "twilio")
            elif status in ("failed", "undelivered"):
                info = {"sid": params.get("MessageSid"), "to": params.get("To", "").split(":")[-1], "status": status,
                        "error_code": params.get("ErrorCode"), "channel": channel}
                await self._emit("sms.failed", info, "twilio")
                await self._emit("integration.error", info, "twilio")
            return Response(status=204)
        text = params.get("Body", "")
        await self._emit("whatsapp.received" if channel == "whatsapp" else "sms.received",
                         {"from": phone, "text": text, "sid": params.get("MessageSid")}, "twilio")
        reply = await self.handle_reply(phone, text)
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response>' + \
                (f"<Message>{escape(reply)}</Message>" if reply else "") + "</Response>"
        return text_response(twiml, "text/xml")

    async def whatsapp_verify(self, req: Request) -> Response:
        q = req.query
        if q.get("hub.mode") == "subscribe" and _eq(q.get("hub.verify_token", ""), self._secret("whatsapp", "verify_token")):
            return text_response(q.get("hub.challenge", ""))
        raise HTTPError(403, "forbidden", "verify token mismatch")

    async def whatsapp(self, req: Request) -> Response:
        secret = self._secret("whatsapp", "app_secret")
        expected = "sha256=" + hmac.new(secret.encode(), req.body, hashlib.sha256).hexdigest()
        if not _eq(expected, req.header("x-hub-signature-256")):
            await self._reject("whatsapp", "signature mismatch")
        body = req.json() or {}
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value") or {}
                for st in val.get("statuses", []):
                    s = st.get("status")
                    if s in ("delivered", "read"):
                        await self._emit("sms.delivered", {"id": st.get("id"), "to": "+" + str(st.get("recipient_id", "")),
                                                           "status": s, "channel": "whatsapp"}, "whatsapp")
                    elif s == "failed":
                        info = {"id": st.get("id"), "to": "+" + str(st.get("recipient_id", "")), "channel": "whatsapp",
                                "errors": st.get("errors")}
                        await self._emit("sms.failed", info, "whatsapp")
                        await self._emit("integration.error", info, "whatsapp")
                for msg in val.get("messages", []):
                    phone = "+" + str(msg.get("from", ""))
                    text = (msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("text") or \
                        ((msg.get("interactive") or {}).get("button_reply") or {}).get("title") or ""
                    await self._emit("whatsapp.received", {"from": phone, "text": text, "id": msg.get("id"),
                                                           "type": msg.get("type")}, "whatsapp")
                    reply = await self.handle_reply(phone, text)
                    n = self.meet.bookings.notifier
                    if reply and n is not None:
                        try:
                            await n.send(phone, reply, "whatsapp")
                        except Exception:  # noqa: BLE001
                            log.exception("whatsapp reply failed")
        return json_response({"ok": True})

    async def handle_reply(self, phone: str, text: str) -> Optional[str]:
        """YES / CONFIRM, CANCEL, RESCHEDULE replies to booking texts."""
        if not self.sms_keywords:
            return None
        word = (text or "").strip().split(" ")[0].upper().strip(".!")
        if word not in ("YES", "Y", "CONFIRM", "CANCEL", "RESCHEDULE", "MOVE", "HELP"):
            return None
        if word == "HELP":
            return "Reply YES to confirm, CANCEL to cancel or RESCHEDULE for a link to pick a new time."
        from .models import BookingStatus
        now = datetime.now(timezone.utc)
        mine = [b for b in await self.meet.storage.list_bookings(start=now - timedelta(hours=1), status=BookingStatus.CONFIRMED)
                if (b.attendee_phone or "").replace(" ", "") == phone and b.end > now]
        if not mine:
            return "We couldn't find an upcoming booking for this number."
        b = sorted(mine, key=lambda x: x.start)[0]
        when = b.start.strftime("%a %d %b %H:%M UTC")
        if word in ("YES", "Y", "CONFIRM"):
            b.metadata["attendee_confirmed"] = time.time()
            await self.meet.storage.save_booking(b)
            await self.meet.emit_webhook("booking.attendee_confirmed", {"booking": b.to_dict(include_secrets=False),
                                                                        "via": "sms"}, tenant_id=b.tenant_id)
            return f"Thanks! You're confirmed for {b.title} on {when}."
        if word == "CANCEL":
            await self.meet.cancel(b.id, reason="cancelled by text message")
            return f"Done: {b.title} on {when} is cancelled."
        return f"Pick a new time here: {self.meet.manage_url(b)}"

    # -- chat bots --------------------------------------------------------------------------------------
    async def command(self, app: str, text: str, user: str) -> str:
        """``/meet [room]``, ``/meet book <host>``, ``/meet today [host]``, ``/meet help``."""
        allow = self.bot_allow.get(app)
        allowed = not allow or user in allow
        await self.meet.emit_webhook("chat_app.command", {"app": app, "user": user, "text": text, "allowed": allowed})
        if not allowed:
            return "Sorry, you're not allowed to use this command."
        parts = (text or "").strip().split()
        sub = parts[0].lower() if parts else ""
        if sub == "help":
            return ("/meet [room] - new meeting links\n/meet book <host> - booking page\n"
                    "/meet today [host] - today's bookings")
        if sub == "book" and len(parts) > 1:
            return f"Book a time: {self.meet.booking_page_url(parts[1])}"
        if sub in ("today", "bookings"):
            from .models import BookingStatus
            now = datetime.now(timezone.utc)
            items = [b for b in await self.meet.storage.list_bookings(start=now, end=now + timedelta(days=1),
                                                                       status=BookingStatus.CONFIRMED)
                     if len(parts) < 2 or b.host_id == parts[1]]
            if not items:
                return "No bookings in the next 24 hours."
            return "\n".join(f"{b.start.strftime('%H:%M UTC')} {b.title} with {b.attendee_name} ({b.host_id})"
                             for b in sorted(items, key=lambda x: x.start)[:20])
        room = re.sub(r"[^a-zA-Z0-9_-]", "-", parts[0])[:60] if parts else None
        r = self.meet.room(room) if room else self.meet.room()
        return (f"Meeting *{r.id}*\nHost: {r.host_link(user or 'Host', ttl='1d')}\n"
                f"Guests: {r.guest_link(ttl='1d')}")

    async def slack(self, req: Request) -> Response:
        secret = self._secret("slack", "signing_secret")
        ts = req.header("x-slack-request-timestamp")
        expected = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + req.body, hashlib.sha256).hexdigest()
        if not ts or not _eq(expected, req.header("x-slack-signature")) or abs(time.time() - int(ts)) > 300:
            await self._reject("slack", "signature mismatch")
        if req.header("content-type").startswith("application/json"):
            body = req.json() or {}
            if body.get("type") == "url_verification":
                return json_response({"challenge": body.get("challenge")})
            await self._emit("chat_app.command", {"app": "slack", "event": body.get("event")}, "slack")
            return json_response({"ok": True})
        form = dict(parse_qsl(req.body.decode()))
        text = await self.command("slack", form.get("text", ""), form.get("user_name") or form.get("user_id", ""))
        return json_response({"response_type": "ephemeral", "text": text})

    async def telegram(self, req: Request) -> Response:
        if not _eq(req.header("x-telegram-bot-api-secret-token"), self._secret("telegram", "secret_token")):
            await self._reject("telegram", "secret token mismatch")
        msg = (req.json() or {}).get("message") or {}
        text = msg.get("text") or ""
        if not text.startswith("/meet"):
            return json_response({"ok": True})
        user = (msg.get("from") or {}).get("username") or str((msg.get("from") or {}).get("id", ""))
        reply = await self.command("telegram", text.split(" ", 1)[1] if " " in text else "", user)
        # answer inside the webhook response: no extra API call needed
        return json_response({"method": "sendMessage", "chat_id": (msg.get("chat") or {}).get("id"), "text": reply,
                              "disable_web_page_preview": True})

    async def discord(self, req: Request) -> Response:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self._secret("discord", "public_key")))
        try:
            key.verify(bytes.fromhex(req.header("x-signature-ed25519")),
                       req.header("x-signature-timestamp").encode() + req.body)
        except (InvalidSignature, ValueError):
            await self._reject("discord", "signature mismatch")
        body = req.json() or {}
        if body.get("type") == 1:
            return json_response({"type": 1})  # PING -> PONG
        opts = {o["name"]: o.get("value") for o in (body.get("data") or {}).get("options", [])}
        text = " ".join(str(v) for v in opts.values() if v)
        user = ((body.get("member") or {}).get("user") or body.get("user") or {}).get("username", "")
        return json_response({"type": 4, "data": {"content": await self.command("discord", text, user), "flags": 64}})

    # -- CRM --------------------------------------------------------------------------------------------
    async def hubspot(self, req: Request) -> Response:
        secret = self._secret("hubspot", "client_secret")
        ts = req.header("x-hubspot-request-timestamp")
        uri = self.meet.base_url + req.path
        mac = base64.b64encode(hmac.new(secret.encode(), (req.method + uri).encode() + req.body + ts.encode(),
                                        hashlib.sha256).digest()).decode()
        if not ts or not _eq(mac, req.header("x-hubspot-signature-v3")) or abs(time.time() * 1000 - int(ts)) > 300_000:
            await self._reject("hubspot", "signature mismatch")
        events = json.loads(req.body or b"[]")
        for e in events if isinstance(events, list) else [events]:
            kind = str(e.get("subscriptionType", ""))
            event = "crm.contact_deleted" if kind == "contact.deletion" else \
                ("crm.contact_updated" if kind.startswith("contact.") else "crm.event")
            await self._emit(event, {"crm": "hubspot", "type": kind, "object_id": e.get("objectId"),
                                     "property": e.get("propertyName"), "value": e.get("propertyValue")}, "hubspot")
        return Response(status=204)

    async def pipedrive(self, req: Request) -> Response:
        cfg = self.secrets.get("pipedrive") or {}
        if not cfg.get("user"):
            raise HTTPError(404, "not_configured", "pipedrive webhooks are not set up")
        expected = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg.get('password', '')}".encode()).decode()
        if not _eq(expected, req.header("authorization")):
            await self._reject("pipedrive", "bad credentials")
        body = req.json() or {}
        meta = body.get("meta") or {}
        obj, action = meta.get("object") or meta.get("entity"), meta.get("action")
        action = {"added": "create", "updated": "change", "deleted": "delete"}.get(action, action)
        event = "crm.event"
        if obj == "person":
            event = "crm.contact_deleted" if action == "delete" else "crm.contact_updated"
        await self._emit(event, {"crm": "pipedrive", "object": obj, "action": action, "id": meta.get("id") or meta.get("entity_id"),
                                 "current": body.get("current") or body.get("data"), "previous": body.get("previous")},
                         "pipedrive")
        return json_response({"ok": True})

    async def salesforce(self, req: Request) -> Response:
        org = self._secret("salesforce", "org_id")
        ns = {"s": "http://schemas.xmlsoap.org/soap/envelope/", "o": "http://soap.sforce.com/2005/09/outbound"}
        root = ET.fromstring(req.body)
        notes = root.find(".//o:notifications", ns)
        if notes is None or not _eq((notes.findtext("o:OrganizationId", "", ns) or "")[:15], org[:15]):
            await self._reject("salesforce", "organization id mismatch")
        for n in notes.findall("o:Notification", ns):
            sobj = n.find("o:sObject", ns)
            if sobj is None:
                continue
            kind = (sobj.get("{http://www.w3.org/2001/XMLSchema-instance}type") or "").split(":")[-1]
            fields = {child.tag.split("}")[-1]: child.text for child in sobj}
            event = "crm.contact_updated" if kind in ("Contact", "Lead") else "crm.event"
            await self._emit(event, {"crm": "salesforce", "object": kind, "id": fields.get("Id"), "fields": fields}, "salesforce")
        ack = ('<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
               '<soapenv:Body><notificationsResponse xmlns="http://soap.sforce.com/2005/09/outbound"><Ack>true</Ack>'
               '</notificationsResponse></soapenv:Body></soapenv:Envelope>')
        return text_response(ack, "text/xml")

    async def zoho(self, req: Request) -> Response:
        token = req.query.get("token") or req.header("x-nodemeet-token")
        if not _eq(token or "", self._secret("zoho", "token")):
            await self._reject("zoho", "token mismatch")
        try:
            body = req.json() or {}
        except Exception:  # noqa: BLE001
            body = dict(parse_qsl(req.body.decode()))
        module = str(body.get("module", ""))
        await self._emit("crm.contact_updated" if module in ("Contacts", "Leads") else "crm.event",
                         {"crm": "zoho", "module": module, "data": body}, "zoho")
        return json_response({"ok": True})
