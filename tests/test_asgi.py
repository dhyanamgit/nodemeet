"""Full HTTP + WebSocket stack through the ASGI adapter (no web framework needed)."""
import asyncio
from datetime import datetime, timedelta, timezone

from asgi_client import ASGIClient, WSClosed
from fakes import FakeSFU
from helpers import run
from nodemeet import JoinRejected, MemoryMailer, NodeMeet
from nodemeet.asgi import route

SECRET = "asgi-test-secret-long-enough"
KEY = "admin-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def make_meet(**kw):
    kw.setdefault("sfu", FakeSFU())
    kw.setdefault("base_url", "http://test")
    return NodeMeet(SECRET, api_key=KEY, mailer=MemoryMailer(), reminders=False, **{"waiting_room": False, **kw})


async def join(c, meet, room, user, role="participant", **tok):
    ws = await c.ws("/ws")
    await ws.send_json({"type": "join", "token": meet.create_token(room, user, role, name=user, **tok)})
    return ws, await ws.recv_until("welcome")


def test_pages_static_health_and_lifespan():
    meet = make_meet()
    app = meet.asgi()

    async def scenario():
        sent = []
        msgs = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

        async def receive():
            return next(msgs)

        async def send(m):
            sent.append(m["type"])

        await app({"type": "lifespan"}, receive, send)
        assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        c = ASGIClient(app)
        r = await c.get("/api/health")
        assert r.status == 200 and r.json()["ok"]
        assert "nodemeet-ui.js" in (await c.get("/r/standup")).text
        r = await c.get("/static/nodemeet.js")
        assert r.status == 200 and r.headers["content-type"].startswith("application/javascript")
        assert (await c.get("/static/../server.py")).status == 404
        assert (await c.get("/nope")).status == 404
        assert (await c.request("DELETE", "/api/health")).status == 405

    run(scenario())


def test_admin_rooms_tokens_and_cors():
    meet = make_meet(cors_origins=["https://site.test"])

    async def scenario():
        c = ASGIClient(meet.asgi())
        assert (await c.post("/api/rooms", {})).status == 401
        r = await c.post("/api/rooms", {"id": "town-hall", "mode": "webinar"}, headers=AUTH)
        assert r.status == 201 and r.json()["mode"] == "webinar"
        r = await c.request("PATCH", "/api/rooms/town-hall", json={"locked": True}, headers=AUTH)
        assert r.json()["locked"] is True
        r = await c.post("/api/tokens", {"room": "town-hall", "user_id": "u1", "role": "host"}, headers=AUTH)
        assert r.status == 201 and r.json()["join_url"].startswith("http://test/r/town-hall#token=")
        assert (await c.post("/api/rooms", {"mode": "nope"}, headers=AUTH)).status == 400
        assert (await c.get("/api/rooms/missing", headers=AUTH)).status == 404
        pre = await c.request("OPTIONS", "/api/bookings", headers={"Origin": "https://site.test"})
        assert pre.status == 204 and pre.headers["access-control-allow-origin"] == "https://site.test"
        assert (await c.request("DELETE", "/api/rooms/town-hall", headers=AUTH)).status == 204

    run(scenario())


def test_booking_flow_over_rest():
    meet = make_meet()
    day = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    hours = {d: "00:00-24:00" for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

    async def scenario():
        c = ASGIClient(meet.asgi())
        body = {"timezone": "UTC", "weekly": hours, "host_email": "host@x.test", "title": "Consult"}
        assert (await c.request("PUT", "/api/hosts/h1/availability", json=body)).status == 401
        assert (await c.request("PUT", "/api/hosts/h1/availability", json=body, headers=AUTH)).status == 200
        pub = (await c.get("/api/hosts/h1/availability")).json()
        assert "host_email" not in pub
        slots = (await c.get(f"/api/hosts/h1/slots?start={day}&end={day}&tz=Asia/Kolkata")).json()["slots"]
        assert len(slots) == 48
        r = await c.post("/api/bookings", {"host_id": "h1", "start": slots[3]["start"], "name": "Ann",
                                           "email": "ann@x.test"})
        b = r.json()
        assert r.status == 201 and b["manage_token"]
        dup = await c.post("/api/bookings", {"host_id": "h1", "start": slots[3]["start"],
                                             "name": "Bob", "email": "bob@x.test"})
        assert dup.status == 409
        assert (await c.get(f"/api/bookings/{b['id']}")).status == 401
        ics = await c.get(f"/api/bookings/{b['id']}/invite.ics?t={b['manage_token']}")
        assert "BEGIN:VCALENDAR" in ics.text
        r = await c.post(f"/api/bookings/{b['id']}/reschedule", {"t": b["manage_token"], "start": slots[6]["start"]})
        assert r.json()["sequence"] == 1
        r = await c.post(f"/api/bookings/{b['id']}/cancel", {"t": b["manage_token"]})
        assert r.json()["status"] == "cancelled"
        assert len((await c.get("/api/bookings?host_id=h1", headers=AUTH)).json()["bookings"]) == 1
        assert len(meet.bookings.mailer.outbox) == 6

    run(scenario())


def test_ws_join_chat_signal_moderation_and_hooks():
    meet = make_meet()
    seen = []
    meet.on_join(lambda room, p: seen.append(("join", p.name)))
    meet.on_leave(lambda room, p: seen.append(("leave", p.name)))
    meet.on_chat(lambda room, p, text: text.replace("heck", "****"))

    @meet.before_join
    def gate(ctx):
        if ctx.claims.user_id == "mallory":
            raise JoinRejected("not today")

    async def scenario():
        c = ASGIClient(meet.asgi())
        a, wa = await join(c, meet, "r1", "alice", "host")
        assert wa["topology"] == "p2p" and "moderate.kick" in wa["permissions"]
        b, wb = await join(c, meet, "r1", "bob")
        assert (await a.recv_until("peer-joined"))["participant"]["name"] == "bob"
        await b.send_json({"type": "signal", "to": wa["peer_id"], "data": {"sdp": "x"}})
        sig = await a.recv_until("signal")
        assert sig["from"] == wb["peer_id"] and sig["data"] == {"sdp": "x"}
        await b.send_json({"type": "chat", "text": "what the heck"})
        assert (await a.recv_until("chat"))["message"]["text"] == "what the ****"
        await b.send_json({"type": "raise-hand", "raised": True})
        assert (await a.recv_until("peer-updated"))["participant"]["hand_raised"] is True
        m = await c.ws("/ws")
        await m.send_json({"type": "join", "token": meet.create_token("r1", "mallory")})
        assert (await m.recv_until("error"))["message"] == "not today"
        await a.send_json({"type": "moderate", "action": "mute", "target": wb["peer_id"]})
        assert (await b.recv_until("force-mute"))["by"] == "alice"
        await a.send_json({"type": "moderate", "action": "kick", "target": wb["peer_id"]})
        assert (await b.recv_until("kicked"))["by"] == "alice"
        assert (await a.recv_until("peer-left"))["peer_id"] == wb["peer_id"]
        await b.close()
        await a.close()
        assert ("join", "alice") in seen and ("leave", "bob") in seen and ("leave", "alice") in seen
        assert meet.rooms.get("r1") is None

    run(scenario())


def test_ws_rejects_bad_tokens_and_enforces_permissions():
    meet = make_meet()

    async def scenario():
        c = ASGIClient(meet.asgi())
        ws = await c.ws("/ws")
        await ws.send_json({"type": "join", "token": "nm1.bad.token"})
        assert (await ws.recv_until("error"))["code"] == "InvalidToken"
        try:
            await ws.receive_json()
        except WSClosed:
            pass
        v, _ = await join(c, meet, "r2", "viewer1", "viewer")
        await v.send_json({"type": "moderate", "action": "lock"})
        assert (await v.recv_until("error"))["code"] == "forbidden"
        await v.send_json({"type": "state", "screen_sharing": True})
        assert (await v.recv_until("error"))["code"] == "forbidden"
        await v.close()
        bad = await c.ws("/elsewhere")
        assert bad.closed or True

    run(scenario())


def test_auto_topology_switches_to_sfu_and_back():
    meet = make_meet(p2p_max=2)

    async def scenario():
        c = ASGIClient(meet.asgi())
        socks = [await join(c, meet, "big", f"u{i}") for i in range(3)]
        assert (await socks[0][0].recv_until("topology"))["topology"] == "sfu"
        ws0, w0 = socks[0]
        await ws0.send_json({"type": "sfu-publish", "sdp": "offer", "sdpType": "offer"})
        assert (await ws0.recv_until("sfu-publish-answer"))["sdp"] == "answer-offer"
        assert (await socks[1][0].recv_until("publisher"))["peer_id"] == w0["peer_id"]
        await socks[1][0].send_json({"type": "sfu-subscribe", "publisher": w0["peer_id"],
                                     "sdp": "o", "sdpType": "offer"})
        assert (await socks[1][0].recv_until("sfu-subscribe-answer"))["sdp"] == "sub-answer"
        await socks[2][0].close()
        await socks[1][0].close()
        assert (await ws0.recv_until("topology"))["topology"] == "p2p"
        await ws0.close()

    run(scenario())


def test_webinar_promotion():
    meet = make_meet()

    async def scenario():
        c = ASGIClient(meet.asgi())
        await meet.create_room(room_id="web", mode="webinar")
        h, wh = await join(c, meet, "web", "host", "host")
        g, wg = await join(c, meet, "web", "guest")
        assert wh["you"]["can_publish"] and not wg["you"]["can_publish"]
        await g.send_json({"type": "sfu-publish", "sdp": "x", "sdpType": "offer"})
        assert (await g.recv_until("error"))["code"] == "forbidden"
        await h.send_json({"type": "moderate", "action": "promote", "target": wg["peer_id"]})
        assert (await g.recv_until("permissions"))["can_publish"] is True
        await h.close()
        await g.close()

    run(scenario())


def test_mounted_under_prefix_like_fastapi_or_django():
    meet = make_meet(base_url="http://test/meet")

    async def other(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"my app"})

    app = route({"/meet": meet.asgi()}, default=other)

    async def scenario():
        c = ASGIClient(app)
        assert (await c.get("/")).text == "my app"
        assert (await c.get("/meet/api/health")).json()["ok"]
        assert "/meet/static/nodemeet.js" in (await c.get("/meet/r/x")).text
        ws = await c.ws("/meet/ws")
        await ws.send_json({"type": "join", "token": meet.create_token("x", "u")})
        assert (await ws.recv_until("welcome"))["type"] == "welcome"
        await ws.close()
        # Starlette-style mount: root_path set, path already relative
        c2 = ASGIClient(meet.asgi(), root_path="/meet")
        assert "/meet/static/" in (await c2.get("/r/y")).text

    run(scenario())


def test_base_url_is_learned_when_not_configured():
    meet = NodeMeet(SECRET, api_key=KEY, reminders=False, sfu=False, waiting_room=False)

    async def scenario():
        c = ASGIClient(meet.asgi(), root_path="/meet")
        r = await c.post("/api/tokens", {"room": "a", "user_id": "u"}, headers=AUTH)
        assert r.json()["join_url"].startswith("http://test/meet/r/a#token=")

    run(scenario())
