"""Calendar sync, payments, SMS/WhatsApp and SSO against fake HTTP transports
(the request shapes follow each provider's public API docs)."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from asgi_client import ASGIClient
from helpers import run
from nodemeet import MemoryMailer, NodeMeet
from nodemeet.integrations.calendars import CalDAVCalendar, CalendarService, GoogleCalendar, OutlookCalendar, _ics_times
from nodemeet.integrations.http_client import HTTPClient, HTTPResult
from nodemeet.integrations.payments import PaymentService, RazorpayPayments, StripePayments
from nodemeet.integrations.sms import MemoryNotifier, TwilioNotifier
from nodemeet.scheduling.availability import Availability
from nodemeet.sso import OIDCProvider, SSOService
from nodemeet.tokens import sign_blob

SECRET = "integrations-secret-long-enough"


class Fake:
    """Routes (method, url-prefix) -> response; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                data = resp(url, headers, body) if callable(resp) else resp
                status, payload = data if isinstance(data, tuple) else (200, data)
                raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                return HTTPResult(status, raw, {})
        return HTTPResult(404, b"{}", {})


def make(**kw):
    return NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=False,
                    base_url="http://test", waiting_room=False, **kw)


def monday_10(tz="UTC"):
    now = datetime.now(timezone.utc)
    d = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    return d.replace(hour=10, minute=0, second=0, microsecond=0)


def test_google_calendar_connect_busy_and_event_sync():
    mon = monday_10()
    fake = Fake({
        ("POST", "https://oauth2.googleapis.com/token"): {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        ("GET", "https://openidconnect.googleapis.com/v1/userinfo"): {"email": "lee@gmail.com"},
        ("POST", "https://www.googleapis.com/calendar/v3/freeBusy"): {"calendars": {"primary": {"busy": [
            {"start": mon.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": (mon + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}]}}},
        ("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events"): {"id": "evt1"},
        ("PATCH", "https://www.googleapis.com/calendar/v3/calendars/primary/events/evt1"): {"id": "evt1"},
        ("DELETE", "https://www.googleapis.com/calendar/v3/calendars/primary/events/evt1"): (204, ""),
    })
    meet = make()
    _cal = CalendarService(meet, [GoogleCalendar("cid", "csecret", http=HTTPClient(fake))])

    async def scenario():
        await meet.bookings.set_availability(Availability.from_hours("lee", "UTC", {"mon-fri": "09:00-17:00"}))
        c = ASGIClient(meet.asgi())
        r = await c.request("GET", "/api/calendars/google/connect?host_id=lee", headers={"Authorization": "Bearer k"})
        url = urlparse(r.json()["url"])
        q = parse_qs(url.query)
        assert url.netloc == "accounts.google.com" and q["access_type"] == ["offline"]
        assert q["redirect_uri"] == ["http://test/api/calendars/google/callback"]
        cb = await c.request("GET", f"/api/calendars/google/callback?code=abc&state={q['state'][0]}")
        assert cb.status == 200 and "lee@gmail.com" in cb.text
        listed = (await c.request("GET", "/api/hosts/lee/calendars", headers={"Authorization": "Bearer k"})).json()
        assert listed["calendars"][0]["account"] == "lee@gmail.com" and "access_token" not in listed["calendars"][0]
        slots = await meet.bookings.find_slots("lee", mon.date(), mon.date())
        starts = {s.start for s in slots}
        assert mon not in starts and mon + timedelta(hours=1) in starts      # Google busy blocks 10:00
        b = await meet.bookings.book("lee", mon + timedelta(hours=2), attendee_name="Ravi", attendee_email="ravi@x.com")
        create = [x for x in fake.calls if x[0] == "POST" and "/events" in x[1]][0]
        body = json.loads(create[3])
        assert body["attendees"][0]["email"] == "ravi@x.com" and body["location"].startswith("http://test/r/")
        assert (await meet.storage.get_booking(b.id)).metadata["calendar_events"] == {"google": "evt1"}
        await meet.bookings.reschedule(b.id, mon + timedelta(hours=3))
        await meet.bookings.cancel(b.id)
        assert [x[0] for x in fake.calls if "/events/evt1" in x[1]] == ["PATCH", "DELETE"]

    run(scenario())


def test_outlook_busy_and_caldav_parsing():
    mon = monday_10()
    fake = Fake({("GET", "https://graph.microsoft.com/v1.0/me/calendarView"): {"value": [
        {"start": {"dateTime": mon.strftime("%Y-%m-%dT%H:%M:%S") + ".0000000"},
         "end": {"dateTime": (mon + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S") + ".0000000"}, "showAs": "busy"},
        {"start": {"dateTime": "2030-01-01T00:00:00"}, "end": {"dateTime": "2030-01-01T01:00:00"}, "showAs": "free"}]}})
    out = OutlookCalendar("id", "s", http=HTTPClient(fake))

    async def scenario():
        busy = await out.busy({"access_token": "t", "expires_at": time.time() + 999}, mon, mon + timedelta(days=1))
        assert busy == [(mon, mon + timedelta(minutes=30))]                # "free" events ignored
        assert fake.calls[0][2]["Prefer"] == 'outlook.timezone="UTC"'

    run(scenario())
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;TZID=Asia/Kolkata:20261005T100000\r\nDTEND;TZID=Asia/Kolkata:20261005T"
           "110000\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART:20261005T120000Z\r\nDTEND:20261005T123000Z\r\nTRANSP:TRANSPARENT\r\n"
           "END:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261006\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    times = _ics_times(ics)
    assert times[0] == (datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc), datetime(2026, 10, 5, 5, 30, tzinfo=timezone.utc))
    assert len(times) == 2 and times[1][1] - times[1][0] == timedelta(days=1)   # transparent skipped, all-day kept

    report = ('<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop>'
              '<cal:calendar-data>BEGIN:VCALENDAR&#13;\nBEGIN:VEVENT&#13;\nDTSTART:20261005T090000Z&#13;\nDTEND:20261005T100000Z'
              '&#13;\nEND:VEVENT&#13;\nEND:VCALENDAR</cal:calendar-data></d:prop></d:propstat></d:response></d:multistatus>')
    dav = CalDAVCalendar(HTTPClient(Fake({("REPORT", "https://dav.example/cal/"): (207, report)})))

    async def caldav():
        busy = await dav.busy({"url": "https://dav.example/cal/", "username": "u", "password": "p"},
                              datetime(2026, 10, 5, tzinfo=timezone.utc), datetime(2026, 10, 6, tzinfo=timezone.utc))
        assert busy == [(datetime(2026, 10, 5, 9, tzinfo=timezone.utc), datetime(2026, 10, 5, 10, tzinfo=timezone.utc))]

    run(caldav())


def _stripe_sig(secret, body, ts=None):
    ts = ts or int(time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def test_stripe_payment_holds_slot_then_confirms():
    fake = Fake({("POST", "https://api.stripe.com/v1/checkout/sessions"): {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"}})
    meet = make()
    sms = MemoryNotifier()
    meet.bookings.notifier = sms
    PaymentService(meet, StripePayments("sk_test", "whsec", http=HTTPClient(fake)))
    mon = monday_10()

    async def scenario():
        await meet.bookings.set_availability(Availability.from_hours("doc", "UTC", {"mon-fri": "09:00-17:00"},
                                                                      price=50000, currency="INR"))
        c = ASGIClient(meet.asgi())
        r = await c.request("POST", "/api/bookings", json={"host_id": "doc", "start": mon.isoformat(), "name": "Ravi",
                                                           "email": "ravi@x.com", "phone": "+919812345678"})
        data = r.json()
        assert r.status == 201 and data["payment_url"] == "https://checkout.stripe.com/c/cs_1"
        form = parse_qs(fake.calls[0][3].decode())
        assert form["line_items[0][price_data][unit_amount]"] == ["50000"] and form["line_items[0][price_data][currency]"] == ["inr"]
        bid = data["booking"]["id"] if "booking" in data else data["id"]
        assert not meet.bookings.mailer.outbox and not sms.outbox                 # nothing sent before payment
        assert not await meet.bookings.is_available("doc", mon, 30)       # but the slot is held
        tok = meet.booking_join_url(await meet.bookings.get(bid)).split("#token=")[1]
        ws = await c.ws("/ws"); await ws.send_json({"type": "join", "token": tok})
        assert (await ws.recv_until("error"))["code"] == "payment_required"
        event = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "id": "cs_1", "payment_status": "paid", "payment_intent": "pi_9", "amount_total": 50000,
            "metadata": {"booking_id": bid}}}}).encode()
        bad = await c.request("POST", "/api/payments/stripe/webhook", headers={"Stripe-Signature": "t=1,v1=00"}, body=event)
        assert bad.status == 400
        ok = await c.request("POST", "/api/payments/stripe/webhook", headers={"Stripe-Signature": _stripe_sig("whsec", event)}, body=event)
        assert ok.json()["status"] == "paid"
        b = await meet.bookings.get(bid)
        assert b.payment["payment_id"] == "pi_9" and not b.awaiting_payment
        assert meet.bookings.mailer.outbox and sms.outbox[0]["to"] == "+919812345678" and "Booked:" in sms.outbox[0]["text"]

    run(scenario())


def test_unpaid_holds_are_released_and_razorpay_signature():
    meet = make()
    PaymentService(meet, RazorpayPayments("rzp_id", "rzp_secret", "rzp_wh", http=HTTPClient(Fake({
        ("POST", "https://api.razorpay.com/v1/payment_links"): {"id": "plink_1", "short_url": "https://rzp.io/i/abc"}}))))
    mon = monday_10()

    async def scenario():
        await meet.bookings.set_availability(Availability.from_hours("doc", "UTC", {"mon-fri": "09:00-17:00"}, price=1000))
        b = await meet.bookings.book("doc", mon, attendee_name="A", attendee_email="a@x.com")
        assert b.payment["url"] == "https://rzp.io/i/abc"
        released = await meet.bookings.release_unpaid(datetime.now(timezone.utc) + timedelta(hours=1))
        assert [x.id for x in released] == [b.id]
        assert await meet.bookings.is_available("doc", mon, 30)
        prov = meet.bookings.payments.provider
        body = json.dumps({"event": "payment_link.paid", "payload": {"payment_link": {"entity": {
            "id": "plink_1", "notes": {"booking_id": b.id}}}, "payment": {"entity": {"id": "pay_1", "amount": 1000}}}}).encode()
        sig = hmac.new(b"rzp_wh", body, hashlib.sha256).hexdigest()
        assert prov.parse_webhook({"x-razorpay-signature": sig}, body) == (b.id, "pay_1", 1000)
        late = await meet.bookings.mark_paid(b.id, provider="razorpay", payment_id="pay_1")
        assert late.payment["status"] == "paid_after_expiry"               # flagged for a refund

    run(scenario())


def test_twilio_sms_and_whatsapp_request_shape():
    fake = Fake({("POST", "https://api.twilio.com/"): {"sid": "SM1"}})
    n = TwilioNotifier("AC1", "tok", sms_from="+15550001111", whatsapp_from="+14155238886", http=HTTPClient(fake))

    async def scenario():
        await n.send("+919812345678", "hi", "sms")
        await n.send("+919812345678", "hi", "whatsapp")
        f1, f2 = (parse_qs(c[3].decode()) for c in fake.calls)
        assert f1["From"] == ["+15550001111"] and f2["To"] == ["whatsapp:+919812345678"]
        assert fake.calls[0][1].endswith("/Accounts/AC1/Messages.json") and fake.calls[0][2]["Authorization"].startswith("Basic ")

    run(scenario())


def test_oidc_sso_login_to_meeting_and_admin_session():
    fake = Fake({
        ("GET", "https://idp.example/.well-known/openid-configuration"): {
            "authorization_endpoint": "https://idp.example/auth", "token_endpoint": "https://idp.example/token",
            "userinfo_endpoint": "https://idp.example/userinfo"},
        ("POST", "https://idp.example/token"): {"access_token": "AT"},
        ("GET", "https://idp.example/userinfo"): {"sub": "u1", "email": "Ana@Acme.com", "name": "Ana", "groups": ["eng"]},
    })
    meet = make()
    SSOService(meet, [OIDCProvider("okta", "cid", "sec", issuer="https://idp.example", http=HTTPClient(fake))],
               role_for=lambda user, room: "host" if "eng" in user["groups"] else "viewer",
               admin_emails=["ana@acme.com"], allowed_domains=["acme.com"])

    async def scenario():
        c = ASGIClient(meet.asgi())
        r = await c.request("GET", "/sso/okta/login?room=standup")
        loc = urlparse(r.headers.get("location") or r.headers.get("Location"))
        q = parse_qs(loc.query)
        assert loc.netloc == "idp.example" and q["code_challenge_method"] == ["S256"]
        cb = await c.request("GET", f"/sso/okta/callback?code=xyz&state={q['state'][0]}")
        target = cb.headers.get("location") or cb.headers.get("Location")
        assert target.startswith("http://test/r/standup#token=")
        claims = meet.tokens.verify(target.split("#token=")[1])
        assert claims.user_id == "ana@acme.com" and claims.role == "host" and claims.meta["sso"] == "okta"
        token_call = [x for x in fake.calls if x[1] == "https://idp.example/token"][0]
        assert "code_verifier" in parse_qs(token_call[3].decode())
        r = await c.request("GET", "/sso/okta/login?admin=1")
        state = parse_qs(urlparse(r.headers.get("location") or r.headers.get("Location")).query)["state"][0]
        adm = await c.request("GET", f"/sso/okta/callback?code=xyz&state={state}")
        session = (adm.headers.get("location") or adm.headers.get("Location")).split("#session=")[1]
        api = await c.request("GET", "/api/rooms", headers={"Authorization": f"Bearer {session}"})
        assert api.status == 200                                     # admin dashboard session works on the API
        forged = sign_blob(NodeMeet("another-secret-entirely-xx", sfu=False).tokens, {"email": "x"}, purpose="admin")
        assert (await c.request("GET", "/api/rooms", headers={"Authorization": f"Bearer {forged}"})).status == 401

    run(scenario())
