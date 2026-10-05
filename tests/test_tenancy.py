"""Per-customer API keys: isolation, scopes, revocation, rate limits, webhooks."""
from datetime import datetime, timedelta, timezone

from asgi_client import ASGIClient
from helpers import run
from nodemeet import MemoryMailer, NodeMeet, WebhookDispatcher, verify_signature

MASTER = {"Authorization": "Bearer master-key"}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def make():
    calls = []

    async def transport(url, body, headers, timeout):
        calls.append((url, body, headers))
        return 200

    meet = NodeMeet("tenant-test-secret-long", waiting_room=False, api_key="master-key", base_url="http://t",
                    reminders=False, sfu=False, mailer=MemoryMailer(),
                    webhooks=WebhookDispatcher(transport=transport, backoff=0))
    return meet, calls


async def new_key(c, tenant, **extra):
    r = await c.post("/api/keys", {"tenant_id": tenant, "name": f"{tenant} key", **extra}, headers=MASTER)
    assert r.status == 201, r.text
    return r.json()["key"]


def test_tenants_are_isolated():
    meet, _ = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        acme, globex = await new_key(c, "acme"), await new_key(c, "globex")
        assert (await c.get("/api/me", headers=bearer(acme))).json()["tenant_id"] == "acme"
        r = await c.post("/api/rooms", {"id": "acme-standup"}, headers=bearer(acme))
        assert r.status == 201 and r.json()["tenant_id"] == "acme"
        # globex can't see, change, steal or mint tokens for acme's room
        assert (await c.get("/api/rooms/acme-standup", headers=bearer(globex))).status == 404
        assert (await c.post("/api/rooms", {"id": "acme-standup"}, headers=bearer(globex))).status == 409
        assert (await c.post("/api/tokens", {"room": "acme-standup", "user_id": "x"},
                             headers=bearer(globex))).status == 404
        assert (await c.get("/api/rooms", headers=bearer(globex))).json()["rooms"] == []
        assert len((await c.get("/api/rooms", headers=MASTER)).json()["rooms"]) == 1
        # hosts + bookings
        body = {"timezone": "UTC", "weekly": {d: "00:00-24:00" for d in
                                              ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}
        assert (await c.request("PUT", "/api/hosts/dr-a/availability", json=body,
                                headers=bearer(acme))).status == 200
        assert (await c.request("PUT", "/api/hosts/dr-a/availability", json=body,
                                headers=bearer(globex))).status == 409
        day = (datetime.now(timezone.utc) + timedelta(days=2)).date()
        slot = (await c.get(f"/api/hosts/dr-a/slots?start={day}&end={day}")).json()["slots"][0]
        b = (await c.post("/api/bookings", {"host_id": "dr-a", "start": slot["start"], "name": "Z",
                                            "email": "z@x.test"})).json()
        assert b["tenant_id"] == "acme"
        assert len((await c.get("/api/bookings", headers=bearer(acme))).json()["bookings"]) == 1
        assert (await c.get("/api/bookings", headers=bearer(globex))).json()["bookings"] == []
        assert (await c.get(f"/api/bookings/{b['id']}", headers=bearer(globex))).status == 401

    run(scenario())


def test_tokens_are_bound_to_the_tenant():
    meet, _ = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        acme = await new_key(c, "acme")
        tok = (await c.post("/api/tokens", {"room": "acme-room", "user_id": "u1"},
                            headers=bearer(acme))).json()
        assert tok["tenant_id"] == "acme"
        assert (await meet.storage.get_room("acme-room")).tenant_id == "acme"
        ws = await c.ws("/ws")
        await ws.send_json({"type": "join", "token": tok["token"]})
        assert (await ws.recv_until("welcome"))["room"]["id"] == "acme-room"
        await ws.close()
        forged = meet.create_token("acme-room", "evil", tenant="globex")
        ws2 = await c.ws("/ws")
        await ws2.send_json({"type": "join", "token": forged})
        assert (await ws2.recv_until("error"))["code"] == "InvalidToken"

    run(scenario())


def test_scopes_revocation_expiry_and_rate_limit():
    meet, _ = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        ro = await new_key(c, "acme", scopes=["rooms:read"])
        assert (await c.get("/api/rooms", headers=bearer(ro))).status == 200
        r = await c.post("/api/rooms", {"id": "x"}, headers=bearer(ro))
        assert r.status == 403 and "rooms:write" in r.json()["message"]
        assert (await c.post("/api/keys", {"tenant_id": "acme"}, headers=bearer(ro))).status == 403
        limited = await new_key(c, "acme", rate_limit_per_minute=2)
        codes = [(await c.get("/api/rooms", headers=bearer(limited))).status for _ in range(3)]
        assert codes == [200, 200, 429]
        keys = (await c.get("/api/keys?tenant_id=acme", headers=MASTER)).json()["keys"]
        assert len(keys) == 2 and all("key_hash" not in k for k in keys)
        kid = next(k["id"] for k in keys if k["scopes"] == ["rooms:read"])
        assert (await c.request("DELETE", f"/api/keys/{kid}", headers=MASTER)).status == 200
        assert (await c.get("/api/rooms", headers=bearer(ro))).status == 401
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        expired = await new_key(c, "acme", expires_at=past)
        assert (await c.get("/api/rooms", headers=bearer(expired))).status == 401
        assert (await c.get("/api/rooms", headers=bearer("nmk_made_up"))).status == 401
        bad = await c.post("/api/keys", {"tenant_id": "acme", "scopes": ["root"]}, headers=MASTER)
        assert bad.status == 400

    run(scenario())


def test_tenant_webhooks_only_receive_their_own_events():
    meet, calls = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        acme, globex = await new_key(c, "acme"), await new_key(c, "globex")
        wh = (await c.post("/api/webhooks", {"url": "https://acme.test/hook",
                                             "events": ["room.created"]}, headers=bearer(acme))).json()
        await c.post("/api/webhooks", {"url": "https://globex.test/hook"}, headers=bearer(globex))
        await c.post("/api/rooms", {"id": "a1"}, headers=bearer(acme))
        await meet.webhooks.close()
        urls = [u for u, _, _ in calls]
        assert urls == ["https://acme.test/hook"]
        url, body, headers = calls[0]
        assert verify_signature(wh["secret"], body, headers["X-NodeMeet-Signature"])
        listed = (await c.get("/api/webhooks", headers=bearer(globex))).json()["webhooks"]
        assert len(listed) == 1 and "secret" not in listed[0]
        assert (await c.request("DELETE", f"/api/webhooks/{wh['id']}", headers=bearer(globex))).status == 404
        assert (await c.request("DELETE", f"/api/webhooks/{wh['id']}", headers=bearer(acme))).status == 204

    run(scenario())
