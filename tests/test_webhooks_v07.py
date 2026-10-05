"""0.7: 120+ outgoing events, delivery log / redelivery / test / feed, and inbound webhooks
from Zoom, Webex, Microsoft, Google, Twilio, WhatsApp, Slack, Telegram, Discord, HubSpot,
Pipedrive, Salesforce, Zoho plus the generic actions API."""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from asgi_client import ASGIClient
from helpers import run
from nodemeet import MemoryMailer, NodeMeet
from nodemeet.events import CATALOG, wants
from nodemeet.integrations.http_client import HTTPClient, HTTPResult
from nodemeet.integrations.sms import MemoryNotifier
from nodemeet.integrations.webhooks import sign_payload

SECRET = "webhooks-v07-secret-long-enough!!"


def make(**kw):
    kw.setdefault("waiting_room", False)
    return NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=False,
                    base_url="http://test", **kw)


def capture(meet):
    seen = []
    meet.on_event("*")(lambda event, data: seen.append((event, data)))
    return seen


def names(seen):
    return [e for e, _ in seen]


def next_monday(hour=10):
    now = datetime.now(timezone.utc)
    d = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


ADMIN = {"Authorization": "Bearer k"}


def test_catalogue_and_patterns():
    assert len(CATALOG) >= 110
    assert wants(None, "booking.created") and not wants(None, "poll.voted")       # noisy skipped by default
    assert wants(["*"], "meeting.ended") and not wants(["*"], "participant.reaction")
    assert wants(["poll.*"], "poll.voted") and wants(["*.failed"], "crm.failed") and not wants(["booking.*"], "crm.failed")
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        ev = (await c.request("GET", "/api/webhooks/events")).json()["events"]
        assert {"event": "meeting.ended", "description": CATALOG["meeting.ended"][0], "noisy": False, "group": "meeting"} in ev
        ok = await c.request("POST", "/api/webhooks", headers=ADMIN, json={"url": "https://x.test/h", "events": ["booking.*", "*.failed", "custom.deal_won"]})
        assert ok.status == 201
        bad = await c.request("POST", "/api/webhooks", headers=ADMIN, json={"url": "https://x.test/h", "events": ["meeting.endd"]})
        assert bad.status == 400 and "meeting.ended" in bad.text

    run(scenario())


def test_meeting_attendance_collaboration_and_moderation_events():
    meet = make()
    seen = capture(meet)
    meet.hours("ada", "mon-fri 9-17")

    async def scenario():
        await meet.startup()
        b = await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com")
        c = ASGIClient(meet.asgi())

        async def join(token):
            ws = await c.ws("/ws")
            await ws.send_json({"type": "join", "token": token})
            ws.welcome = await ws.recv_until("welcome")
            return ws

        host = await join(meet.booking_join_url(b, "host").split("#token=")[1])
        guest = await join(meet.booking_join_url(b).split("#token=")[1])
        extra = await join(meet.create_token(b.room, "zed", name="Zed"))
        target = extra.welcome["peer_id"]
        await host.send_json({"type": "poll-create", "question": "Lunch?", "options": ["Yes", "No"]})
        await guest.send_json({"type": "qa-ask", "text": "When is the break?"})
        await guest.send_json({"type": "state", "screen_sharing": True})
        await guest.send_json({"type": "raise-hand", "raised": True})
        await host.send_json({"type": "moderate", "action": "kick", "target": target})
        await host.send_json({"type": "ping", "ts": 1})
        await host.recv_until("pong")
        for ws in (guest, host, extra):
            await ws.close()
        for _ in range(50):
            if "meeting.ended" in names(seen):
                break
            await __import__("asyncio").sleep(0.02)
        await meet.webhooks.drain()
        n = names(seen)
        for e in ("meeting.started", "booking.host_joined", "booking.attendee_joined", "booking.completed",
                  "poll.created", "qa.asked", "screen_share.started", "participant.hand_raised",
                  "participant.moderated", "participant.kicked", "meeting.ended"):
            assert e in n, e
        ended = dict(seen)["meeting.ended"]
        assert ended["attendee_count"] == 3 and ended["peak_participants"] == 3 and ended["duration_seconds"] >= 0
        assert dict(seen)["poll.created"]["question"] == "Lunch?" and dict(seen)["poll.created"]["by"]["name"]
        fresh = await meet.bookings.get(b.id)
        assert set(fresh.metadata["attendance"]) == {"host", "attendee"}
        assert "email.sent" in n                                           # confirmation emails
        await meet.shutdown()

    run(scenario())


def test_no_show_and_payment_expiry_events():
    meet = make()
    seen = capture(meet)
    meet.hours("ada", "mon-sun 0-23:59")

    async def scenario():
        await meet.startup()
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=60)
        b = await meet.book("ada", start, name="Ravi", email="ravi@x.com", check_availability=False)
        done = await meet.bookings.check_attendance(now=start + timedelta(hours=2))
        assert [x.id for x in done] == [b.id]
        await meet.webhooks.drain()
        assert dict(seen)["booking.no_show"]["missing"] == ["host", "attendee"]
        assert await meet.bookings.check_attendance(now=start + timedelta(hours=3)) == []   # only once
        await meet.shutdown()

    run(scenario())


def test_delivery_log_redeliver_test_and_long_poll_feed():
    calls = []
    state = {"fail": True}

    async def transport(url, body, headers, timeout):
        calls.append((url, json.loads(body), headers))
        return 500 if state["fail"] else 200

    meet = make()
    meet.webhooks.transport, meet.webhooks.retries, meet.webhooks.backoff = transport, 0, 0
    meet.add_webhook("https://hooks.test/in", "whsec_1", events=["room.*", "webhook.test"])

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        await meet.create_room(room_id="r1")
        await meet.webhooks.drain()
        rows = (await c.request("GET", "/api/webhooks/deliveries?failed=1", headers=ADMIN)).json()["deliveries"]
        assert rows and rows[0]["event"] in ("room.created", "room.updated") and rows[0]["status"] == 500 and not rows[0]["ok"]
        state["fail"] = False
        again = await c.request("POST", f"/api/webhooks/deliveries/{rows[0]['id']}/redeliver", headers=ADMIN)
        assert again.json()["ok"] and calls[-1][1]["id"] == rows[0]["delivery_id"]          # same delivery id
        t = await c.request("POST", "/api/webhooks/test", headers=ADMIN, json={})
        assert t.json()["results"][0]["ok"] and calls[-1][2]["X-NodeMeet-Event"] == "webhook.test"
        assert (await c.request("GET", "/api/webhooks/deliveries", headers={})).status in (401, 403)
        feed = (await c.request("GET", "/api/events?after=0&events=room.*", headers=ADMIN)).json()
        assert feed["events"][0]["event"] == "room.created" and feed["next"] >= 1
        nxt = feed["latest"]
        await meet.create_room(room_id="r2")
        later = (await c.request("GET", f"/api/events?after={nxt}&wait=1", headers=ADMIN)).json()
        assert [e["data"]["id"] for e in later["events"] if e["event"] == "room.created"] == ["r2"]
        await meet.shutdown()

    run(scenario())


def _stripe_sig(secret, body):
    ts = int(time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def test_stripe_refund_and_dispute_events_update_the_booking():
    async def fake(method, url, headers, body):
        return HTTPResult(200, json.dumps({"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"}).encode(), {})

    meet = make()
    seen = capture(meet)
    meet.hours("ada", "mon-fri 9-17", price="₹500")
    meet.add_payments("stripe", "sk_test", "whsec", http=HTTPClient(fake), cancel_on_refund=True)

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        b = await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com")

        async def send(event):
            raw = json.dumps(event).encode()
            return await c.request("POST", "/api/payments/stripe/webhook", headers={"Stripe-Signature": _stripe_sig("whsec", raw)}, body=raw)

        paid = await send({"type": "checkout.session.completed", "data": {"object": {"id": "cs_1", "payment_status": "paid",
                          "payment_intent": "pi_9", "amount_total": 50000, "metadata": {"booking_id": b.id}}}})
        assert paid.json()["status"] == "paid"
        dispute = await send({"type": "charge.dispute.created", "data": {"object": {"charge": "ch_1", "payment_intent": "pi_9",
                             "amount": 50000, "reason": "fraudulent"}}})
        assert dispute.json()["booking"] == b.id                                   # found via the payment id
        refund = await send({"type": "charge.refunded", "data": {"object": {"id": "ch_1", "payment_intent": "pi_9",
                            "amount": 50000, "amount_refunded": 50000, "refunded": True}}})
        assert refund.json()["event"] == "payment.refunded"
        fresh = await meet.bookings.get(b.id)
        assert fresh.payment["status"] == "refunded" and fresh.payment["amount_refunded"] == 50000
        assert fresh.status.value == "cancelled"                                 # cancel_on_refund=True
        await meet.webhooks.drain()
        n = names(seen)
        assert {"payment.checkout_created", "booking.paid", "payment.disputed", "payment.refunded", "booking.cancelled"} <= set(n)
        assert dict(seen)["payment.disputed"]["reason"] == "fraudulent"
        await meet.shutdown()

    run(scenario())


def test_inbound_conferencing_calendar_and_crm_webhooks():
    meet = make()
    seen = capture(meet)
    meet.hours("ada", "mon-fri 9-17", email="ada@acme.com")
    meet.add_conferencing("jitsi")
    meet.inbound.configure("zoom", webhook_secret="zsec")
    meet.inbound.configure("webex", webhook_secret="wsec")
    meet.inbound.configure("microsoft", client_state="cstate")
    meet.inbound.configure("google-calendar", channel_token="gtok")
    meet.inbound.configure("hubspot", client_secret="hsec")
    meet.inbound.configure("pipedrive", user="pd", password="pw")
    meet.inbound.configure("salesforce", org_id="00D5g000000ABCDEAZ")
    meet.inbound.configure("zoho", token="ztok")

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        b = await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com")
        await meet.storage.put_record("conf_ref", "zoom|85123", {"booking_id": b.id})
        v = await c.request("POST", "/api/inbound/zoom", json={"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
        assert v.json()["encryptedToken"] == hmac.new(b"zsec", b"abc", hashlib.sha256).hexdigest()
        body = json.dumps({"event": "meeting.participant_joined", "payload": {"object": {"id": 85123, "topic": "Meeting",
                           "participant": {"user_name": "Ravi", "email": "ravi@x.com"}}}}).encode()
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(b"zsec", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        ok = await c.request("POST", "/api/inbound/zoom", headers={"x-zm-request-timestamp": ts, "x-zm-signature": sig}, body=body)
        assert ok.status == 200
        bad = await c.request("POST", "/api/inbound/zoom", headers={"x-zm-request-timestamp": ts, "x-zm-signature": "v0=00"}, body=body)
        assert bad.status == 401
        wbody = json.dumps({"resource": "meetings", "event": "ended", "data": {"meetingId": "wx1"}}).encode()
        w = await c.request("POST", "/api/inbound/webex", headers={"x-spark-signature": hmac.new(b"wsec", wbody, hashlib.sha1).hexdigest()}, body=wbody)
        assert w.status == 200
        val = await c.request("POST", "/api/inbound/microsoft?validationToken=hello%20world", body=b"")
        assert val.text == "hello world"
        await meet.storage.put_record("calendar_watch", "sub-1", {"host_id": "ada", "provider": "outlook"})
        g = await c.request("POST", "/api/inbound/microsoft", json={"value": [{"subscriptionId": "sub-1", "clientState": "cstate",
                                                                               "changeType": "updated", "resource": "Users/x/Events/1"}]})
        assert g.status == 202
        gc = await c.request("POST", "/api/inbound/google-calendar", headers={"x-goog-channel-token": "gtok",
                             "x-goog-resource-state": "exists", "x-goog-channel-id": "nm-1"}, body=b"")
        assert gc.status == 200
        hb = json.dumps([{"subscriptionType": "contact.propertyChange", "objectId": 501, "propertyName": "email",
                          "propertyValue": "new@x.com"}]).encode()
        hts = str(int(time.time() * 1000))
        hsig = base64.b64encode(hmac.new(b"hsec", b"POSThttp://test/api/inbound/hubspot" + hb + hts.encode(), hashlib.sha256).digest()).decode()
        h = await c.request("POST", "/api/inbound/hubspot", headers={"x-hubspot-request-timestamp": hts, "x-hubspot-signature-v3": hsig}, body=hb)
        assert h.status == 204
        pd = await c.request("POST", "/api/inbound/pipedrive", headers={"Authorization": "Basic " + base64.b64encode(b"pd:pw").decode()},
                             json={"meta": {"action": "deleted", "object": "person", "id": 77}, "previous": {"name": "Ravi"}})
        assert pd.status == 200
        soap = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<soapenv:Body><notifications xmlns="http://soap.sforce.com/2005/09/outbound"><OrganizationId>00D5g000000ABCDEAZ</OrganizationId>'
                '<Notification><Id>04l1</Id><sObject xsi:type="sf:Contact" xmlns:sf="urn:sobject.enterprise.soap.sforce.com">'
                '<sf:Id>003X</sf:Id><sf:Email>ravi@x.com</sf:Email></sObject></Notification></notifications></soapenv:Body></soapenv:Envelope>').encode()
        sf = await c.request("POST", "/api/inbound/salesforce", body=soap)
        assert sf.status == 200 and b"<Ack>true</Ack>" in sf.body
        z = await c.request("POST", "/api/inbound/zoho?token=ztok", json={"module": "Contacts", "id": "Z1"})
        assert z.status == 200
        assert (await c.request("POST", "/api/inbound/zoho?token=nope", json={})).status == 401
        assert (await c.request("POST", "/api/inbound/telegram", json={})).status == 404     # not configured
        await meet.webhooks.drain()
        got = dict(seen)
        assert got["conference.participant_joined"]["booking_id"] == b.id and got["conference.participant_joined"]["participant"]["email"] == "ravi@x.com"
        assert got["conference.ended"]["provider"] == "webex"
        assert {d["provider"]: d["host_id"] for e, d in seen if e == "calendar.changed"} == {"outlook": "ada", "google": None}
        assert got["crm.contact_updated"]["crm"] in ("hubspot", "salesforce", "zoho")
        assert got["crm.contact_deleted"]["crm"] == "pipedrive"
        assert [d["fields"]["Email"] for e, d in seen if e == "crm.contact_updated" and d["crm"] == "salesforce"] == ["ravi@x.com"]
        assert names(seen).count("inbound.rejected") == 2
        assert "conference.created" in names(seen)                                 # jitsi on booking
        await meet.shutdown()

    run(scenario())


def _twilio_sig(token, url, params):
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def test_text_replies_confirm_cancel_and_delivery_receipts():
    sent = []

    async def fake(method, url, headers, body):
        sent.append((url, body))
        return HTTPResult(201, b"{}", {})

    meet = make()
    seen = capture(meet)
    meet.hours("ada", "mon-fri 9-17")
    meet.add_sms("twilio", "AC1", "tw-token", "+15550001111", http=HTTPClient(fake),
                 app_secret="wa-secret", verify_token="wa-verify")

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        b = await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com", attendee_phone="+919812345678")
        assert b"StatusCallback=http%3A%2F%2Ftest%2Fapi%2Finbound%2Ftwilio" in sent[0][1]
        url = "http://test/api/inbound/twilio"

        async def text(params):
            return await c.request("POST", "/api/inbound/twilio", body=urlencode(params).encode(),
                                   headers={"Content-Type": "application/x-www-form-urlencoded",
                                            "X-Twilio-Signature": _twilio_sig("tw-token", url, params)})

        st = await text({"MessageSid": "SM1", "MessageStatus": "undelivered", "To": "+919812345678", "ErrorCode": "30003"})
        assert st.status == 204
        yes = await text({"MessageSid": "SM2", "From": "+919812345678", "Body": "yes please", "To": "+15550001111"})
        assert "confirmed" in yes.text and yes.text.startswith("<?xml")
        forged = await c.request("POST", "/api/inbound/twilio", body=b"From=%2B1&Body=CANCEL",
                                 headers={"X-Twilio-Signature": "nope"})
        assert forged.status == 401
        v = await c.request("GET", "/api/inbound/whatsapp?hub.mode=subscribe&hub.verify_token=wa-verify&hub.challenge=42")
        assert v.text == "42"
        wa = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"from": "919812345678", "id": "wamid.1", "type": "text",
                         "text": {"body": "CANCEL"}}], "statuses": [{"id": "wamid.0", "status": "read", "recipient_id": "919812345678"}]}}]}]}).encode()
        w = await c.request("POST", "/api/inbound/whatsapp", body=wa, headers={
            "X-Hub-Signature-256": "sha256=" + hmac.new(b"wa-secret", wa, hashlib.sha256).hexdigest()})
        assert w.status == 200
        fresh = await meet.bookings.get(b.id)
        assert fresh.status.value == "cancelled" and fresh.metadata.get("attendee_confirmed")
        await meet.webhooks.drain()
        n = names(seen)
        for e in ("sms.sent", "sms.failed", "sms.received", "booking.attendee_confirmed", "whatsapp.received",
                  "sms.delivered", "booking.cancelled", "integration.error", "inbound.rejected"):
            assert e in n, e
        await meet.shutdown()

    run(scenario())


def test_chat_bots_and_actions_api():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    dkey = Ed25519PrivateKey.generate()
    dpub = dkey.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    meet = make(inbound_secret="act-secret")
    seen = capture(meet)
    meet.hours("ada", "mon-fri 9-17")
    assert meet.add_bot("slack", "slack-sign") == "http://test/api/inbound/slack"
    meet.add_bot("telegram", "tg-secret")
    meet.add_bot("discord", dpub, allow=["ravi"])

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        form = urlencode({"command": "/meet", "text": "standup", "user_name": "ada"}).encode()
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(b"slack-sign", f"v0:{ts}:".encode() + form, hashlib.sha256).hexdigest()
        s = await c.request("POST", "/api/inbound/slack", body=form, headers={"Content-Type": "application/x-www-form-urlencoded",
                            "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
        out = s.json()
        assert out["response_type"] == "ephemeral" and "http://test/r/standup#token=" in out["text"]
        tg = await c.request("POST", "/api/inbound/telegram", headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                             json={"message": {"text": "/meet book ada", "chat": {"id": 99}, "from": {"username": "ravi"}}})
        assert tg.json() == {"method": "sendMessage", "chat_id": 99, "text": "Book a time: http://test/book/ada",
                             "disable_web_page_preview": True}

        async def discord(payload):
            raw = json.dumps(payload).encode()
            dts = str(int(time.time()))
            return await c.request("POST", "/api/inbound/discord", body=raw, headers={
                "X-Signature-Ed25519": dkey.sign(dts.encode() + raw).hex(), "X-Signature-Timestamp": dts})

        assert (await discord({"type": 1})).json() == {"type": 1}
        d = await discord({"type": 2, "data": {"name": "meet", "options": [{"name": "room", "value": "help"}]},
                           "member": {"user": {"username": "mallory"}}})
        assert "not allowed" in d.json()["data"]["content"] and d.json()["data"]["flags"] == 64

        async def act(action, data, signed=True, headers=None):
            raw = json.dumps({"action": action, "data": data}).encode()
            h = dict(headers or {})
            if signed:
                h["X-NodeMeet-Signature"] = sign_payload("act-secret", raw)
            return await c.request("POST", "/api/inbound/actions", body=raw, headers=h)

        r = await act("booking.create", {"host_id": "ada", "start": next_monday().isoformat(), "name": "Zap", "email": "zap@x.com"})
        assert r.status == 200 and r.json()["result"]["attendee_email"] == "zap@x.com"
        assert (await act("event.emit", {"name": "Deal Won", "data": {"amount": 5000}})).json()["result"] == {"event": "custom.deal_won"}
        link = await act("link.create", {"room_id": "sales", "name": "Lead", "skip_waiting_room": True}, signed=False, headers=ADMIN)
        assert link.json()["result"]["url"].startswith("http://test/r/sales#token=")
        assert (await act("link.create", {"room_id": "x"}, signed=False)).status in (401, 403)
        assert (await act("bookng.create", {})).status == 400
        await meet.webhooks.drain()
        got = dict(seen)
        assert got["custom.deal_won"] == {"amount": 5000}
        assert names(seen).count("chat_app.command") >= 3 and "inbound.received" in names(seen)
        await meet.shutdown()

    run(scenario())
