"""Waiting room (on by default), the join list, and what happens to blocked people."""
from asgi_client import ASGIClient
from fakes import FakeSFU
from helpers import run
from nodemeet import MemoryMailer, NodeMeet

SECRET = "waiting-room-secret-long-enough"


def make(**kw):
    return NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=FakeSFU(),
                    base_url="http://test", **kw)


async def connect(c, meet, room, user, role="participant", **tok):
    ws = await c.ws("/ws")
    await ws.send_json({"type": "join", "token": meet.create_token(room, user, role, name=user, **tok)})
    return ws


def test_waiting_room_is_on_by_default_and_host_admits():
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "r", "ada", "host")
        await host.recv_until("welcome")                       # hosts walk straight in
        bob = await connect(c, meet, "r", "bob")
        waiting = await bob.recv_until("lobby")                # everyone else waits
        update = await host.recv_until("lobby-update")
        assert update["lobby"][0]["name"] == "bob"
        await host.send_json({"type": "moderate", "action": "admit", "target": waiting["peer_id"]})
        await bob.recv_until("admitted")
        assert (await bob.recv_until("welcome"))["room"]["id"] == "r"
        await bob.close(); await host.close()

    run(scenario())


def test_join_list_and_skip_links_bypass_the_waiting_room():
    meet = make()

    async def scenario():
        await meet.create_room(room_id="r", join_list=["Carol@Example.com"])
        c = ASGIClient(meet.asgi())
        carol = await connect(c, meet, "r", "carol@example.com")      # case-insensitive match
        await carol.recv_until("welcome")
        dan = await connect(c, meet, "r", "dan", grant=["room.bypass_lobby"])  # skip_waiting_room link
        await dan.recv_until("welcome")
        await meet.add_to_join_list("r", "erin")
        erin = await connect(c, meet, "r", "erin")
        await erin.recv_until("welcome")
        for ws in (carol, dan, erin):
            await ws.close()

    run(scenario())


def test_waiting_room_can_be_turned_off():
    async def scenario():
        meet = make(waiting_room=False)                        # whole server
        c = ASGIClient(meet.asgi())
        bob = await connect(c, meet, "open-room", "bob")
        await bob.recv_until("welcome")
        await bob.close()
        meet2 = make()
        await meet2.create_room(room_id="r2", waiting_room=False)   # one room
        c2 = ASGIClient(meet2.asgi())
        eve = await connect(c2, meet2, "r2", "eve")
        await eve.recv_until("welcome")
        await eve.close()

    run(scenario())


def test_blocked_people_are_refused_and_hosts_are_told():
    meet = make()
    attempts = []
    meet.on("on_blocked_attempt", lambda room, event: attempts.append(event["user_id"]))

    async def scenario():
        await meet.create_room(room_id="r", join_list=["bob"])
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "r", "ada", "host")
        await host.recv_until("welcome")
        bob = await connect(c, meet, "r", "bob")
        wb = await bob.recv_until("welcome")
        await host.send_json({"type": "moderate", "action": "ban", "target": wb["peer_id"]})
        await bob.recv_until("banned")
        cfg = await meet.storage.get_room("r")
        assert cfg.is_banned("bob") and not cfg.on_join_list("bob")   # ban also drops the join list
        assert cfg.banned_info["bob"]["by"] == "ada"
        again = await connect(c, meet, "r", "bob")              # fresh token, same user
        err = await again.recv_until("error")
        assert err["code"] == "banned" and "can't rejoin" in err["message"]
        alert = await host.recv_until("ban-attempt")
        assert alert["user_id"] == "bob" and "ip" not in alert
        assert attempts == ["bob"]
        await host.send_json({"type": "moderate", "action": "unban", "user_id": "bob"})
        await host.recv_until("room-updated")
        back = await connect(c, meet, "r", "bob")               # unbanned -> waiting room again
        await back.recv_until("lobby")
        await back.close(); await host.close()

    run(scenario())


def test_block_straight_from_the_waiting_room():
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "r", "ada", "host")
        await host.recv_until("welcome")
        spam = await connect(c, meet, "r", "spammer")
        w = await spam.recv_until("lobby")
        await host.send_json({"type": "moderate", "action": "block", "target": w["peer_id"]})
        await spam.recv_until("banned")
        assert (await meet.storage.get_room("r")).is_banned("spammer")
        await host.close()

    run(scenario())


def test_rest_join_list_and_bans():
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        auth = {"Authorization": "Bearer k"}
        r = await c.request("POST", "/api/rooms", json={"id": "cls", "join_list": ["a"]}, headers=auth)
        assert r.status == 201 and r.json()["lobby"] is True and r.json()["allowed_users"] == ["a"]
        r = await c.request("POST", "/api/rooms/cls/join-list", json={"users": ["b"]}, headers=auth)
        assert r.json()["join_list"] == ["a", "b"]
        r = await c.request("POST", "/api/rooms/cls/join-list/remove", json={"users": ["a"]}, headers=auth)
        assert r.json()["join_list"] == ["b"]
        r = await c.request("POST", "/api/tokens", json={"room": "cls", "user_id": "z", "skip_waiting_room": True}, headers=auth)
        assert "room.bypass_lobby" in meet.tokens.verify(r.json()["token"]).grant

    run(scenario())
