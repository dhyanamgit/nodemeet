"""HTTP + WebSocket tests (need aiohttp; run with plain pytest)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from nodemeet import JoinRejected, MemoryMailer, NodeMeet  # noqa: E402
from fakes import FakeSFU  # noqa: E402

SECRET = "server-test-secret-long"
KEY = "admin-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def make_meet(**kw):
    kw.setdefault("sfu", FakeSFU())
    return NodeMeet(SECRET, api_key=KEY, base_url="http://test", mailer=MemoryMailer(),
                    reminders=False, **{"waiting_room": False, **kw})


def run_with_client(meet, fn, app=None):
    async def runner():
        client = TestClient(TestServer(app or meet.app()))
        await client.start_server()
        try:
            await fn(client)
        finally:
            await client.close()
    asyncio.run(runner())


async def recv_until(ws, kind, timeout=3.0):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg["type"] == kind:
            return msg


async def join(client, meet, room, user, role="participant", prefix=""):
    ws = await client.ws_connect(prefix + "/ws")
    await ws.send_json({"type": "join", "token": meet.create_token(room, user, role, name=user)})
    welcome = await recv_until(ws, "welcome")
    return ws, welcome


def test_health_pages_and_static():
    meet = make_meet()

    async def fn(c):
        r = await c.get("/api/health")
        assert r.status == 200 and (await r.json())["ok"] is True
        r = await c.get("/r/standup")
        assert r.status == 200 and "nodemeet-ui.js" in await r.text()
        r = await c.get("/static/nodemeet.js")
        assert r.status == 200 and "NodeMeet" in await r.text()
        r = await c.get("/book/host1")
        assert "booking-widget.js" in await r.text()

    run_with_client(meet, fn)


def test_admin_auth_rooms_and_tokens():
    meet = make_meet()

    async def fn(c):
        assert (await c.post("/api/rooms", json={})).status == 401
        r = await c.post("/api/rooms", json={"id": "town-hall", "mode": "webinar"}, headers=AUTH)
        assert r.status == 201 and (await r.json())["mode"] == "webinar"
        r = await c.patch("/api/rooms/town-hall", json={"locked": True}, headers=AUTH)
        assert (await r.json())["locked"] is True
        r = await c.post("/api/tokens", json={"room": "town-hall", "user_id": "u1", "role": "host"},
                         headers=AUTH)
        data = await r.json()
        assert r.status == 201 and data["join_url"].startswith("http://test/r/town-hall#token=")
        assert meet.tokens.verify(data["token"]).role == "host"
        r = await c.post("/api/rooms", json={"mode": "nope"}, headers=AUTH)
        assert r.status == 400
        assert (await c.get("/api/rooms/missing", headers=AUTH)).status == 404
        assert (await c.delete("/api/rooms/town-hall", headers=AUTH)).status == 204

    run_with_client(meet, fn)


def test_booking_flow_over_rest():
    meet = make_meet()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    hours = {d: "00:00-24:00" for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

    async def fn(c):
        body = {"timezone": "UTC", "weekly": hours, "duration_minutes": 30,
                "host_email": "host@x.test", "title": "Consult"}
        assert (await c.put("/api/hosts/h1/availability", json=body)).status == 401
        r = await c.put("/api/hosts/h1/availability", json=body, headers=AUTH)
        assert r.status == 200
        pub = await (await c.get("/api/hosts/h1/availability")).json()
        assert "host_email" not in pub and pub["title"] == "Consult"
        r = await c.get(f"/api/hosts/h1/slots?start={tomorrow}&end={tomorrow}&tz=Asia/Kolkata")
        slots = (await r.json())["slots"]
        assert len(slots) == 48 and slots[0]["timezone"] == "Asia/Kolkata"
        r = await c.post("/api/bookings", json={"host_id": "h1", "start": slots[3]["start"],
                                                "name": "Ann", "email": "ann@x.test"})
        b = await r.json()
        assert r.status == 201 and b["manage_token"] and b["join_url"].startswith("http://test/r/")
        r = await c.post("/api/bookings", json={"host_id": "h1", "start": slots[3]["start"],
                                                "name": "Bob", "email": "bob@x.test"})
        assert r.status == 409
        assert (await c.get(f"/api/bookings/{b['id']}")).status == 401
        r = await c.get(f"/api/bookings/{b['id']}/invite.ics?t={b['manage_token']}")
        assert r.status == 200 and "BEGIN:VCALENDAR" in await r.text()
        r = await c.post(f"/api/bookings/{b['id']}/reschedule",
                         json={"t": b["manage_token"], "start": slots[6]["start"]})
        assert r.status == 200 and (await r.json())["sequence"] == 1
        r = await c.post(f"/api/bookings/{b['id']}/cancel", json={"t": b["manage_token"]})
        assert (await r.json())["status"] == "cancelled"
        listing = await (await c.get("/api/bookings?host_id=h1", headers=AUTH)).json()
        assert len(listing["bookings"]) == 1
        assert len(meet.bookings.mailer.outbox) == 6  # attendee + host x 3

    run_with_client(meet, fn)


def test_ws_join_chat_signal_and_hooks():
    meet = make_meet()
    seen = []
    meet.on_join(lambda room, p: seen.append(("join", p.name)))
    meet.on_leave(lambda room, p: seen.append(("leave", p.name)))

    @meet.on_chat
    def censor(room, p, text):
        return text.replace("heck", "****")

    @meet.before_join
    def gate(ctx):
        if ctx.claims.user_id == "mallory":
            raise JoinRejected("not today")

    async def fn(c):
        a, wa = await join(c, meet, "r1", "alice", "host")
        assert wa["topology"] == "p2p" and "moderate.kick" in wa["permissions"]
        b, wb = await join(c, meet, "r1", "bob")
        joined = await recv_until(a, "peer-joined")
        assert joined["participant"]["name"] == "bob"
        assert [p["name"] for p in wb["room"]["participants"]] == ["alice", "bob"]
        await b.send_json({"type": "signal", "to": wa["peer_id"], "data": {"sdp": "x"}})
        sig = await recv_until(a, "signal")
        assert sig["from"] == wb["peer_id"] and sig["data"] == {"sdp": "x"}
        await b.send_json({"type": "chat", "text": "what the heck"})
        chat = await recv_until(a, "chat")
        assert chat["message"]["text"] == "what the ****"
        await b.send_json({"type": "raise-hand", "raised": True})
        upd = await recv_until(a, "peer-updated")
        assert upd["participant"]["hand_raised"] is True
        ws = await c.ws_connect("/ws")
        await ws.send_json({"type": "join", "token": meet.create_token("r1", "mallory")})
        err = await recv_until(ws, "error")
        assert err["message"] == "not today"
        await a.send_json({"type": "moderate", "action": "kick", "target": wb["peer_id"]})
        assert (await recv_until(b, "kicked"))["by"] == "alice"
        left = await recv_until(a, "peer-left")
        assert left["peer_id"] == wb["peer_id"]
        await a.close()
        await asyncio.sleep(0.05)
        assert ("join", "alice") in seen and ("leave", "bob") in seen and ("leave", "alice") in seen
        assert meet.rooms.get("r1") is None

    run_with_client(meet, fn)


def test_ws_rejects_bad_token_and_permissions():
    meet = make_meet()

    async def fn(c):
        ws = await c.ws_connect("/ws")
        await ws.send_json({"type": "join", "token": "nm1.bad.token"})
        assert (await recv_until(ws, "error"))["code"] in ("InvalidToken", "invalid_token", "rejected")
        v, _ = await join(c, meet, "r2", "viewer1", "viewer")
        await v.send_json({"type": "moderate", "action": "lock"})
        assert (await recv_until(v, "error"))["code"] == "forbidden"
        await v.send_json({"type": "state", "screen_sharing": True})
        assert (await recv_until(v, "error"))["code"] == "forbidden"
        await v.close()

    run_with_client(meet, fn)


def test_auto_topology_switches_to_sfu_and_back():
    meet = make_meet(p2p_max=2)

    async def fn(c):
        socks = []
        for i in range(3):
            ws, w = await join(c, meet, "big", f"u{i}")
            socks.append((ws, w))
        topo = await recv_until(socks[0][0], "topology")
        assert topo["topology"] == "sfu"
        ws0, w0 = socks[0]
        await ws0.send_json({"type": "sfu-publish", "sdp": "offer", "sdpType": "offer"})
        ans = await recv_until(ws0, "sfu-publish-answer")
        assert ans["sdp"] == "answer-offer"
        pub = await recv_until(socks[1][0], "publisher")
        assert pub["peer_id"] == w0["peer_id"]
        await socks[1][0].send_json({"type": "sfu-subscribe", "publisher": w0["peer_id"],
                                     "sdp": "o", "sdpType": "offer"})
        assert (await recv_until(socks[1][0], "sfu-subscribe-answer"))["sdp"] == "sub-answer"
        await socks[2][0].close()
        await socks[1][0].close()
        back = await recv_until(ws0, "topology")
        assert back["topology"] == "p2p"
        await ws0.close()

    run_with_client(meet, fn)


def test_webinar_only_hosts_publish_and_promotion():
    meet = make_meet()

    async def fn(c):
        await meet.create_room(room_id="web", mode="webinar")
        h, wh = await join(c, meet, "web", "host", "host")
        g, wg = await join(c, meet, "web", "guest")
        assert wh["you"]["can_publish"] and not wg["you"]["can_publish"]
        await g.send_json({"type": "sfu-publish", "sdp": "x", "sdpType": "offer"})
        assert (await recv_until(g, "error"))["code"] == "forbidden"
        await h.send_json({"type": "moderate", "action": "promote", "target": wg["peer_id"]})
        perms = await recv_until(g, "permissions")
        assert perms["can_publish"] is True
        await h.close()
        await g.close()

    run_with_client(meet, fn)


def test_mount_into_existing_app_under_prefix():
    meet = make_meet()
    meet.base_url = "http://test/meet"
    parent = web.Application()

    async def hello(request):
        return web.Response(text="my app")

    parent.router.add_get("/", hello)
    meet.mount(parent, "/meet")

    async def fn(c):
        assert await (await c.get("/")).text() == "my app"
        assert (await (await c.get("/meet/api/health")).json())["ok"]
        page = await (await c.get("/meet/r/x")).text()
        assert "/meet/static/nodemeet.js" in page
        ws, w = await join(c, meet, "x", "u", prefix="/meet")
        assert w["type"] == "welcome"
        await ws.close()

    run_with_client(meet, fn, app=parent)
