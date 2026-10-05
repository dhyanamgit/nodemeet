"""Chat (Slack/Discord/Teams/Google Chat/Telegram), CRM (HubSpot/Salesforce/Pipedrive/Zoho),
PayPal, push (Web Push/FCM/OneSignal/ntfy) and conferencing (Zoom/Teams/Meet/Webex/Jitsi)
against fake HTTP transports that check each provider's documented request shapes."""
import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from asgi_client import ASGIClient
from helpers import run
from nodemeet import MemoryMailer, NodeMeet
from nodemeet.integrations.http_client import HTTPClient, HTTPResult

SECRET = "integrations-v05-secret-long-enough"


class Fake:
    """(method, url-prefix) -> response (dict, (status, dict), or callable); records calls."""

    def __init__(self, routes=None):
        self.routes, self.calls = dict(routes or {}), []

    async def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                data = resp(url, headers, body) if callable(resp) else resp
                status, payload = data if isinstance(data, tuple) else (200, data)
                raw = payload if isinstance(payload, bytes) else (payload.encode() if isinstance(payload, str)
                                                                  else json.dumps(payload).encode())
                return HTTPResult(status, raw, {})
        return HTTPResult(404, b"{}", {})

    def find(self, method, prefix):
        return [c for c in self.calls if c[0] == method and c[1].startswith(prefix)]

    def body(self, call):
        return json.loads(call[3])


def make(**kw):
    return NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=False,
                    base_url="http://test", waiting_room=False, **kw)


def next_monday(hour=10):
    now = datetime.now(timezone.utc)
    d = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


async def book_move_cancel(meet):
    mon = next_monday()
    b = await meet.book("ada", mon, name="Ravi Kumar", email="ravi@x.com")
    await meet.webhooks.drain()
    b = await meet.reschedule(b, mon + timedelta(hours=2))
    await meet.webhooks.drain()
    b = await meet.cancel(b, reason="sick")
    await meet.webhooks.drain()
    return b


# -- chat -------------------------------------------------------------------------------------
def test_chat_apps_get_booking_events():
    fake = Fake({("POST", "https://hooks.slack.com/"): "ok", ("POST", "https://discord.com/"): {"id": "1"},
                 ("POST", "https://acme.webhook.office.com/"): "1", ("POST", "https://chat.googleapis.com/"): {},
                 ("POST", "https://api.telegram.org/"): {"ok": True},
                 ("POST", "https://slack.com/api/chat.postMessage"): {"ok": False, "error": "channel_not_found"}})
    http = HTTPClient(fake)
    meet = make()
    meet.hours("ada", "mon-fri 9-17", email="ada@x.com")
    slack = meet.add_chat("slack", "https://hooks.slack.com/services/T/B/X", http=http)
    discord = meet.add_chat("discord", "https://discord.com/api/webhooks/1/abc", http=http, events=["booking.created"])
    meet.add_chat("teams", "https://acme.webhook.office.com/webhookb2/x", http=http)
    meet.add_chat("google_chat", "https://chat.googleapis.com/v1/spaces/S/messages?key=k", http=http)
    meet.add_chat("telegram", "123:ABC", "-1001", http=http)
    bot = meet.add_chat("slack", token="xoxb-1", channel="#bookings", http=http)

    async def scenario():
        await meet.startup()
        await book_move_cancel(meet)
        await meet.emit_webhook("participant.blocked_attempt", {"room": "r1", "name": "Mallory"})
        await meet.webhooks.drain()
        await meet.shutdown()

    run(scenario())
    s = fake.find("POST", "https://hooks.slack.com/")
    assert [fake.body(c)["text"].split(":")[0] for c in s] == [
        "New booking", "Rescheduled", "Cancelled", "Blocked person tried to rejoin"]
    assert "Ravi Kumar" in fake.body(s[0])["blocks"][0]["text"]["text"]
    assert "<http://test/book/manage/" in fake.body(s[0])["blocks"][0]["text"]["text"]
    d = fake.find("POST", "https://discord.com/")
    assert len(d) == 1 and "wait=true" in d[0][1]                       # events filter: created only
    emb = fake.body(d[0])["embeds"][0]
    assert emb["title"].startswith("New booking") and emb["color"] == 0x22c55e and fake.body(d[0])["allowed_mentions"] == {"parse": []}
    t = fake.body(fake.find("POST", "https://acme.webhook.office.com/")[0])
    assert t["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert t["attachments"][0]["content"]["actions"][0]["type"] == "Action.OpenUrl"
    assert "*New booking" in fake.body(fake.find("POST", "https://chat.googleapis.com/")[0])["text"]
    tg = fake.find("POST", "https://api.telegram.org/bot123:ABC/sendMessage")
    assert fake.body(tg[0])["chat_id"] == "-1001" and fake.body(tg[0])["parse_mode"] == "HTML"
    assert len(slack.sent) == 4 and len(discord.sent) == 1
    # bot token: Slack answers 200 with ok=false -> the failure is logged, never sent
    bot_calls = fake.find("POST", "https://slack.com/api/chat.postMessage")
    assert bot_calls and bot_calls[0][2]["Authorization"] == "Bearer xoxb-1" and not bot.sent
    with pytest.raises(ValueError) as e:
        meet.add_chat("slak", "x")
    assert "slack" in str(e.value)


# -- CRM --------------------------------------------------------------------------------------
def test_hubspot_contact_and_meeting_lifecycle():
    fake = Fake({("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert"): {"results": [{"id": "501"}]},
                 ("POST", "https://api.hubapi.com/crm/v3/objects/meetings"): {"id": "9001"},
                 ("PATCH", "https://api.hubapi.com/crm/v3/objects/meetings/9001"): {"id": "9001"}})
    meet = make()
    meet.hours("ada", "mon-fri 9-17")
    meet.add_crm("hubspot", "pat-1", http=HTTPClient(fake))

    async def scenario():
        await meet.startup()
        b = await book_move_cancel(meet)
        assert b.metadata["crm"]["hubspot"] == {"contact_id": "501", "meeting_id": "9001", "cancelled": True}
        await meet.shutdown()

    run(scenario())
    up = fake.body(fake.calls[0])["inputs"][0]
    assert up["idProperty"] == "email" and up["properties"] == {"email": "ravi@x.com", "firstname": "Ravi", "lastname": "Kumar"}
    mt = fake.body(fake.calls[1])
    assert mt["associations"][0]["to"]["id"] == "501" and mt["associations"][0]["types"][0]["associationTypeId"] == 200
    assert mt["properties"]["hs_meeting_outcome"] == "SCHEDULED" and mt["properties"]["hs_meeting_external_url"].startswith("http://test/r/")
    patches = fake.find("PATCH", "https://api.hubapi.com/crm/v3/objects/meetings/9001")
    assert [fake.body(p)["properties"]["hs_meeting_outcome"] for p in patches] == ["RESCHEDULED", "CANCELED"]
    assert all(c[2]["Authorization"] == "Bearer pat-1" for c in fake.calls)


def test_salesforce_auth_retry_contact_and_event():
    tokens = iter(["tok1", "tok2"])
    state = {"first": True}

    def query(url, h, b):
        if state["first"]:
            state["first"] = False
            return (401, [{"errorCode": "INVALID_SESSION_ID"}])
        return {"records": []}

    fake = Fake({("POST", "https://acme.my.salesforce.com/services/oauth2/token"):
                 lambda u, h, b: {"access_token": next(tokens), "instance_url": "https://acme.my.salesforce.com"},
                 ("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"): query,
                 ("POST", "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Contact"): {"id": "003X"},
                 ("POST", "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Event"): {"id": "00UX"},
                 ("PATCH", "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Event/00UX"): (204, b"")})
    meet = make()
    meet.hours("ada", "mon-fri 9-17")
    meet.add_crm("salesforce", "cid", "csecret", domain="acme.my.salesforce.com", http=HTTPClient(fake))

    async def scenario():
        await meet.startup()
        b = await book_move_cancel(meet)
        assert b.metadata["crm"]["salesforce"]["meeting_id"] == "00UX"
        await meet.shutdown()

    run(scenario())
    assert len(fake.find("POST", "https://acme.my.salesforce.com/services/oauth2/token")) == 2   # re-auth after 401
    q = fake.find("GET", "https://acme.my.salesforce.com/services/data/v61.0/query")[-1][1]
    assert "Email+%3D+%27ravi%40x.com%27" in q
    ev = fake.body(fake.find("POST", "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Event")[0])
    assert ev["WhoId"] == "003X" and ev["StartDateTime"].endswith("Z")
    assert fake.body(fake.find("PATCH", "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Event/00UX")[-1])[
        "Subject"].startswith("[Cancelled]")


def test_pipedrive_reuses_existing_person_and_zoho_tokens():
    fake = Fake({("GET", "https://api.pipedrive.com/v1/persons/search"): {"success": True, "data": {"items": [{"item": {"id": 77}}]}},
                 ("POST", "https://api.pipedrive.com/v1/activities"): {"success": True, "data": {"id": 5}},
                 ("PUT", "https://api.pipedrive.com/v1/activities/5"): {"success": True, "data": {"id": 5}},
                 ("POST", "https://accounts.zoho.in/oauth/v2/token"): {"access_token": "zt", "expires_in": 3600},
                 ("POST", "https://www.zohoapis.in/crm/v6/Contacts/upsert"): {"data": [{"details": {"id": "Z1"}}]},
                 ("POST", "https://www.zohoapis.in/crm/v6/Events"): {"data": [{"details": {"id": "E1"}}]},
                 ("PUT", "https://www.zohoapis.in/crm/v6/Events/E1"): {"data": [{"details": {"id": "E1"}}]}})
    meet = make()
    meet.hours("ada", "mon-fri 9-17")
    meet.add_crm("pipedrive", "pd-token", http=HTTPClient(fake))
    meet.add_crm("zoho", "zid", "zsec", "zrefresh", dc="in", http=HTTPClient(fake))

    async def scenario():
        await meet.startup()
        b = await book_move_cancel(meet)
        assert b.metadata["crm"]["pipedrive"]["contact_id"] == "77" and b.metadata["crm"]["zoho"]["meeting_id"] == "E1"
        await meet.shutdown()

    run(scenario())
    assert not fake.find("POST", "https://api.pipedrive.com/v1/persons")          # found, not duplicated
    act = fake.body(fake.find("POST", "https://api.pipedrive.com/v1/activities")[0])
    assert act["person_id"] == 77 and act["type"] == "meeting" and act["duration"] == "00:30" and act["due_time"] == "10:00"
    assert "api_token=pd-token" in fake.find("POST", "https://api.pipedrive.com/v1/activities")[0][1]
    assert len(fake.find("POST", "https://accounts.zoho.in/oauth/v2/token")) == 1   # token cached
    z = fake.find("POST", "https://www.zohoapis.in/crm/v6/Events")[0]
    assert z[2]["Authorization"] == "Zoho-oauthtoken zt" and fake.body(z)["data"][0]["Who_Id"] == {"id": "Z1"}


# -- PayPal -------------------------------------------------------------------------------------
def test_paypal_checkout_verify_capture_and_confirm():
    verified = {"ok": True}
    fake = Fake({("POST", "https://api-m.sandbox.paypal.com/v1/oauth2/token"): {"access_token": "A21", "expires_in": 32000},
                 ("POST", "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER1/capture"): lambda u, h, b: {
                     "id": "ORDER1", "status": "COMPLETED", "purchase_units": [{"reference_id": "x", "payments": {"captures": [
                         {"id": "CAP1", "custom_id": BID["id"], "amount": {"currency_code": "USD", "value": "19.99"}}]}}]},
                 ("POST", "https://api-m.sandbox.paypal.com/v2/checkout/orders"): {
                     "id": "ORDER1", "status": "PAYER_ACTION_REQUIRED",
                     "links": [{"rel": "self", "href": "x"}, {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1"}]},
                 ("POST", "https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature"):
                     lambda u, h, b: {"verification_status": "SUCCESS" if verified["ok"] else "FAILURE"}})
    BID = {}
    meet = make()
    meet.hours("ada", "mon-fri 9-17", price="$19.99")
    meet.add_payments("paypal", "client", "secret", "WH-1", sandbox=True, http=HTTPClient(fake))
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.paypal.com/v1/notifications/certs/C",
               "PAYPAL-TRANSMISSION-ID": "t1", "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "2026-10-01T10:00:00Z"}

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        r = await c.request("POST", "/api/bookings", json={"host_id": "ada", "start": next_monday().isoformat(),
                                                           "name": "Ravi", "email": "ravi@x.com"})
        data = r.json()
        assert data["payment_url"] == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1"
        BID["id"] = data["booking"]["id"] if "booking" in data else data["id"]
        order = fake.body(fake.find("POST", "https://api-m.sandbox.paypal.com/v2/checkout/orders")[0])
        assert order["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "19.99"}
        assert order["purchase_units"][0]["custom_id"] == BID["id"] and order["intent"] == "CAPTURE"
        event = json.dumps({"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "ORDER1"}}).encode()
        verified["ok"] = False
        bad = await c.request("POST", "/api/payments/paypal/webhook", headers=headers, body=event)
        assert bad.status == 400
        assert (await c.request("POST", "/api/payments/paypal/webhook", body=event)).status == 400   # no headers
        verified["ok"] = True
        ok = await c.request("POST", "/api/payments/paypal/webhook", headers=headers, body=event)
        assert ok.json()["status"] == "paid"
        b = await meet.bookings.get(BID["id"])
        assert b.payment["payment_id"] == "CAP1" and b.payment["amount_paid"] == 1999
        assert meet.bookings.mailer.outbox                                   # confirmation sent after payment
        done = json.dumps({"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
            "id": "CAP1", "status": "COMPLETED", "custom_id": BID["id"], "amount": {"currency_code": "USD", "value": "19.99"}}}).encode()
        again = await c.request("POST", "/api/payments/paypal/webhook", headers=headers, body=done)
        assert again.json()["status"] == "paid"                               # idempotent
        v = fake.body(fake.find("POST", "https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature")[0])
        assert v["webhook_id"] == "WH-1" and v["transmission_id"] == "t1" and v["webhook_event"]["event_type"]
        await meet.shutdown()

    run(scenario())
    assert len(fake.find("POST", "https://api-m.sandbox.paypal.com/v1/oauth2/token")) == 1   # token cached


# -- push ----------------------------------------------------------------------------------------
def test_web_push_rfc8291_vector_and_vapid():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from nodemeet.integrations.push import (_private_key, b64u, decrypt_payload, encrypt_payload,
                                            generate_vapid_keys, unb64u, vapid_header)
    # RFC 8291 Appendix A
    body = encrypt_payload(b"When I grow up, I want to be a watermelon",
                           "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                           "BTBZMqHH6r4Tts7J_aSIgg", salt=unb64u("DGv6ra1nlYgDCS1FRnbzlw"),
                           sender_private=_private_key("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"))
    assert b64u(body) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
    assert decrypt_payload(body, "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94", "BTBZMqHH6r4Tts7J_aSIgg") == \
        b"When I grow up, I want to be a watermelon"
    priv, pub = generate_vapid_keys()
    hdr = vapid_header("https://fcm.googleapis.com/fcm/send/abc", priv, "mailto:a@b.c")
    jwt, k = hdr[len("vapid t="):].split(", k=")
    assert k == pub
    h, c, sig = jwt.split(".")
    claims = json.loads(unb64u(c))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:a@b.c"
    raw = unb64u(sig)
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(pub))
    key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))           # raises if invalid


def test_web_push_subscribe_notify_and_expiry():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from nodemeet.integrations.push import b64u, decrypt_payload
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_priv = b64u(ua.private_numbers().private_value.to_bytes(32, "big"))
    ua_pub = b64u(ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    auth = b64u(b"0123456789abcdef")
    gone = {"flag": False}
    fake = Fake({("POST", "https://fcm.googleapis.com/fcm/send/"): lambda u, h, b: (410, b"") if gone["flag"] else (201, b"")})
    meet = make()
    meet.hours("ada", "mon-fri 9-17")
    meet.room("help-desk", owner="ada")
    meet.add_push(http=HTTPClient(fake))
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/dev1", "keys": {"p256dh": ua_pub, "auth": auth}}

    async def scenario():
        await meet.startup()
        c = ASGIClient(meet.asgi())
        key = (await c.request("GET", "/api/push/key")).json()
        assert key["provider"] == "webpush" and len(base64.urlsafe_b64decode(key["public_key"] + "==")) == 65
        assert (await c.request("POST", "/api/push/subscribe", json={"token": "nope", "subscription": sub})).status == 401
        tok = meet.create_token("anything", "ada", "host")
        r = await c.request("POST", "/api/push/subscribe", json={"token": tok, "subscription": sub})
        assert r.status == 201 and r.json()["user_id"] == "ada"
        bad = await c.request("POST", "/api/push/subscribe", json={"token": tok, "subscription": {"endpoint": "http://x"}})
        assert bad.status == 400
        await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com")     # -> host gets a push
        await meet.webhooks.drain()
        call = fake.calls[-1]
        assert call[2]["Content-Encoding"] == "aes128gcm" and call[2]["Authorization"].startswith("vapid t=")
        msg = json.loads(decrypt_payload(call[3], ua_priv, auth))
        assert msg["title"].startswith("New booking") and "Ravi" in msg["body"] and msg["url"].startswith("http://test/book/manage/")
        g = ASGIClient(meet.asgi())                                              # someone waits -> owner pinged
        await meet.room("help-desk").update(waiting_room=True)
        ws = await g.ws("/ws")
        await ws.send_json({"type": "join", "token": meet.create_token("help-desk", "bob", name="Bob")})
        await ws.recv_until("lobby")
        await meet.webhooks.drain()
        msg = json.loads(decrypt_payload(fake.calls[-1][3], ua_priv, auth))
        assert msg["title"] == "Someone is waiting" and msg["urgency"] == "high" and "help-desk" in msg["body"]
        await ws.close()
        gone["flag"] = True                                                       # browser unsubscribed -> 410
        assert await meet.push.notify("ada", "hi", "there") == 0
        assert await meet.push.subscriptions("ada") == []
        await meet.shutdown()

    run(scenario())


def test_fcm_onesignal_ntfy():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from nodemeet.integrations.push import unb64u
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    sa = {"project_id": "proj-1", "client_email": "svc@proj-1.iam.gserviceaccount.com", "private_key": pem,
          "token_uri": "https://oauth2.googleapis.com/token"}
    fake = Fake({("POST", "https://oauth2.googleapis.com/token"): {"access_token": "ya29", "expires_in": 3600},
                 ("POST", "https://fcm.googleapis.com/v1/projects/proj-1/messages:send"):
                     lambda u, h, b: (404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}})
                     if b"dead" in b else {"name": "projects/proj-1/messages/1"},
                 ("POST", "https://api.onesignal.com/notifications"): {"id": "n1"},
                 ("POST", "https://ntfy.example.com/"): {"id": "x"}})
    http = HTTPClient(fake)

    async def scenario():
        meet = make()
        meet.add_push("fcm", json.dumps(sa), http=http)
        await meet.startup()
        await meet.push.subscribe("ada", {"token": "good-device"})
        await meet.push.subscribe("ada", {"token": "dead-device"})
        assert await meet.push.notify("ada", "Hello", "World", url="https://app/x") == 1
        assert len(await meet.push.subscriptions("ada")) == 1                  # dead token removed
        await meet.shutdown()
        meet2 = make()
        meet2.add_push("onesignal", "app-1", "os-key", http=http)
        await meet2.push.notify("ada", "T", "B", url="https://u")
        meet3 = make()
        meet3.add_push("ntfy", "https://ntfy.example.com", topic_prefix="acme-", http=http)
        await meet3.push.notify("ravi@x.com", "Reminder", "Starts in 15 min", url="https://u")

    run(scenario())
    tok = parse_qs(fake.find("POST", "https://oauth2.googleapis.com/token")[0][3].decode())
    assert tok["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    h, c, s = tok["assertion"][0].split(".")
    assert json.loads(unb64u(c))["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    rsa_key.public_key().verify(unb64u(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
    send = fake.body(fake.find("POST", "https://fcm.googleapis.com/v1/projects/proj-1/messages:send")[0])["message"]
    assert send["notification"] == {"title": "Hello", "body": "World"} and send["webpush"]["fcm_options"]["link"] == "https://app/x"
    os_ = fake.find("POST", "https://api.onesignal.com/notifications")[0]
    assert os_[2]["Authorization"] == "Key os-key" and fake.body(os_)["include_aliases"] == {"external_id": ["ada"]}
    nt = fake.find("POST", "https://ntfy.example.com/")[0]
    assert nt[1] == "https://ntfy.example.com/acme-ravi_x_com" and nt[2]["Title"] == "Reminder" and nt[3] == b"Starts in 15 min"


# -- conferencing ---------------------------------------------------------------------------------
def test_zoom_meeting_replaces_link_in_emails_and_follows_changes():
    fake = Fake({("POST", "https://zoom.us/oauth/token"): {"access_token": "zt", "expires_in": 3599},
                 ("POST", "https://api.zoom.us/v2/users/ada@acme.com/meetings"): {
                     "id": 8512345678, "join_url": "https://us06web.zoom.us/j/8512345678?pwd=abc", "start_url": "https://s", "password": "abc"},
                 ("PATCH", "https://api.zoom.us/v2/meetings/8512345678"): (204, b""),
                 ("DELETE", "https://api.zoom.us/v2/meetings/8512345678"): (204, b"")})
    meet = make()
    meet.hours("ada", "mon-fri 9-17", email="ada@acme.com")
    meet.add_conferencing("zoom", "acct", "cid", "csec", http=HTTPClient(fake))
    meet.add_crm("hubspot", "pat", http=HTTPClient(Fake({
        ("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert"): {"results": [{"id": "1"}]},
        ("POST", "https://api.hubapi.com/crm/v3/objects/meetings"): {"id": "2"},
        ("PATCH", "https://api.hubapi.com/"): {}})))

    async def scenario():
        await meet.startup()
        b = await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com")
        assert b.metadata["conference"]["join_url"].startswith("https://us06web.zoom.us/j/")
        assert meet.booking_join_url(b) == b.metadata["conference"]["join_url"]
        mail = meet.bookings.mailer.outbox[0]
        assert "us06web.zoom.us/j/8512345678" in mail.text
        assert "zoom.us" in meet.bookings.ics(b)
        crm_url = b.metadata["crm"]["hubspot"]
        assert crm_url["meeting_id"] == "2"                         # CRM ran after Zoom (got the Zoom link)
        await meet.reschedule(b, next_monday(13))
        b = await meet.cancel(b)
        assert b.metadata["conference"]["deleted"]
        await meet.shutdown()

    run(scenario())
    auth = fake.calls[0]
    assert parse_qs(auth[3].decode()) == {"grant_type": ["account_credentials"], "account_id": ["acct"]}
    assert auth[2]["Authorization"] == "Basic " + base64.b64encode(b"cid:csec").decode()
    body = fake.body(fake.find("POST", "https://api.zoom.us/v2/users/ada@acme.com/meetings")[0])
    assert body["type"] == 2 and body["duration"] == 30 and body["start_time"].endswith("T10:00:00Z")
    assert fake.body(fake.find("PATCH", "https://api.zoom.us/v2/meetings/8512345678")[0])["start_time"].endswith("T13:00:00Z")
    assert fake.find("DELETE", "https://api.zoom.us/v2/meetings/8512345678")
    assert len(fake.find("POST", "https://zoom.us/oauth/token")) == 1


def test_teams_meet_webex_jitsi_and_also_mode():
    fake = Fake({("POST", "https://login.microsoftonline.com/tid/oauth2/v2.0/token"): {"access_token": "mt", "expires_in": 3600},
                 ("POST", "https://graph.microsoft.com/v1.0/users/host@acme.com/onlineMeetings"): {
                     "id": "MSO1", "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/19%3ameeting"},
                 ("POST", "https://oauth2.googleapis.com/token"): {"access_token": "gt", "expires_in": 3600},
                 ("POST", "https://meet.googleapis.com/v2/spaces"): {"name": "spaces/abc", "meetingUri": "https://meet.google.com/abc-defg-hij"},
                 ("POST", "https://webexapis.com/v1/meetings"): {"id": "wx1", "webLink": "https://acme.webex.com/acme/j.php?MTID=m1", "password": "pw"}})
    http = HTTPClient(fake)

    async def one(kind, *args, mode="replace", **kw):
        meet = make()
        meet.hours("ada", "mon-fri 9-17", email="ada@acme.com")
        meet.add_conferencing(kind, *args, mode=mode, http=http, **kw)
        await meet.startup()
        b = await meet.book("ada", next_monday(), name="Ravi", email="ravi@x.com")
        url = meet.booking_join_url(b)
        await meet.shutdown()
        return b, url

    async def scenario():
        b, url = await one("teams", "tid", "cid", "sec", organizer="host@acme.com")
        assert url.startswith("https://teams.microsoft.com/l/meetup-join/")
        b, url = await one("google_meet", "gid", "gsec", "grefresh")
        assert url == "https://meet.google.com/abc-defg-hij"
        b, url = await one("webex", "wx-token")
        assert url.startswith("https://acme.webex.com/")
        b, url = await one("jitsi")
        assert url.startswith("https://meet.jit.si/Meeting-") and len(url) > 40
        b, url = await one("jitsi", mode="also")
        assert url.startswith("http://test/r/") and b.metadata["conference"]["join_url"].startswith("https://meet.jit.si/")

    run(scenario())
    tm = fake.body(fake.find("POST", "https://graph.microsoft.com/")[0])
    assert tm["startDateTime"].endswith("Z") and tm["subject"] == "Meeting"
    assert parse_qs(fake.find("POST", "https://login.microsoftonline.com/")[0][3].decode())["scope"] == ["https://graph.microsoft.com/.default"]
    wx = fake.find("POST", "https://webexapis.com/v1/meetings")[0]
    assert wx[2]["Authorization"] == "Bearer wx-token" and fake.body(wx)["hostEmail"] == "ada@acme.com"


def test_env_and_config_wiring_for_new_integrations():
    import os
    env = {"NODEMEET_CHAT": "slack,discord", "SLACK_WEBHOOK_URL": "https://hooks.slack.com/x",
           "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x", "NODEMEET_CRM": "hubspot",
           "HUBSPOT_TOKEN": "pat", "NODEMEET_CONFERENCING": "jitsi", "NODEMEET_PUSH": "ntfy"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        meet = NodeMeet.from_env(dotenv=False, secret=SECRET, sfu=False, reminders=False)
        assert {"chat:slack", "chat:discord", "crm:hubspot", "conferencing:jitsi", "push:ntfy"} <= set(meet._integrations)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    meet = NodeMeet.from_dict({"secret": SECRET, "sfu": False, "reminders": False,
                               "chat": [{"app": "teams", "credentials": ["https://acme.webhook.office.com/x"]}],
                               "conferencing": "jitsi", "push": {"app": "ntfy", "topic_prefix": "x-"}})
    assert {"chat:teams", "conferencing:jitsi", "push:ntfy"} <= set(meet._integrations)
    with pytest.raises(ValueError) as e:
        make().add_crm("hubspot")
    assert "HUBSPOT_TOKEN" in str(e.value)
