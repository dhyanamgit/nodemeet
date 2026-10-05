"""Two nodemeet servers sharing a broker behave like one (simulated with MemoryHub)."""
from asgi_client import ASGIClient
from fakes import FakeSFU
from helpers import run
from nodemeet import MemoryStorage, NodeMeet
from nodemeet.broker import MemoryBroker, MemoryHub

SECRET = "cluster-test-secret-long"


def two_nodes(**kw):
    hub, storage = MemoryHub(), MemoryStorage()
    nodes = [NodeMeet(SECRET, waiting_room=False, storage=storage, broker=MemoryBroker(hub, node_id=f"n{i}"),
                      base_url="http://t", reminders=False, **kw) for i in (1, 2)]
    return nodes, [ASGIClient(n.asgi()) for n in nodes]


async def join(c, meet, room, user, role="participant"):
    ws = await c.ws("/ws")
    await ws.send_json({"type": "join", "token": meet.create_token(room, user, role, name=user)})
    return ws, await ws.recv_until("welcome")


def test_participants_on_different_servers_see_each_other():
    (m1, m2), (c1, c2) = two_nodes(sfu=False)

    async def scenario():
        a, wa = await join(c1, m1, "r", "alice", "host")
        b, wb = await join(c2, m2, "r", "bob")
        assert [p["name"] for p in wb["room"]["participants"]] == ["alice", "bob"]
        assert (await a.recv_until("peer-joined"))["participant"]["name"] == "bob"
        # WebRTC signaling across servers
        await a.send_json({"type": "signal", "to": wb["peer_id"], "data": {"sdp": "offer"}})
        assert (await b.recv_until("signal"))["from"] == wa["peer_id"]
        await b.send_json({"type": "signal", "to": wa["peer_id"], "data": {"sdp": "answer"}})
        assert (await a.recv_until("signal"))["data"] == {"sdp": "answer"}
        # chat + presence state
        await b.send_json({"type": "chat", "text": "hi from node 2"})
        assert (await a.recv_until("chat"))["message"]["text"] == "hi from node 2"
        assert m1.rooms.get("r").chat[-1].text == "hi from node 2"
        await b.send_json({"type": "raise-hand"})
        assert (await a.recv_until("peer-updated"))["participant"]["hand_raised"] is True
        assert m1.rooms.get("r").participants[wb["peer_id"]].hand_raised is True
        # moderation of a participant on the other server
        await a.send_json({"type": "moderate", "action": "mute", "target": wb["peer_id"]})
        assert (await b.recv_until("force-mute"))["by"] == "alice"
        await a.send_json({"type": "moderate", "action": "kick", "target": wb["peer_id"]})
        assert (await b.recv_until("kicked"))["by"] == "alice"
        assert (await a.recv_until("peer-left"))["peer_id"] == wb["peer_id"]
        assert wb["peer_id"] not in m1.rooms.get("r").participants
        await a.close()
        assert m1.rooms.get("r") is None and m2.rooms.get("r") is None

    run(scenario())


def test_topology_is_shared_across_servers():
    (m1, m2), (c1, c2) = two_nodes(sfu=FakeSFU(), p2p_max=2)

    async def scenario():
        a, _ = await join(c1, m1, "big", "a")
        b, _ = await join(c2, m2, "big", "b")
        c, wc = await join(c1, m1, "big", "c")  # third person -> SFU
        assert wc["topology"] == "sfu"
        assert (await b.recv_until("topology"))["topology"] == "sfu"
        assert m2.rooms.get("big").topology == "sfu"
        late, wl = await join(c2, m2, "big", "d")
        assert wl["topology"] == "sfu"
        for ws in (a, b, c, late):
            await ws.close()

    run(scenario())


def test_stale_presence_from_crashed_server_is_ignored():
    (m1, m2), (c1, c2) = two_nodes(sfu=False)

    async def scenario():
        await m1.startup()
        await m1.broker.hset("presence:r", "ghost", '{"peer_id": "ghost", "user_id": "g", '
                             '"name": "Ghost", "role": "participant", "node_id": "dead-node"}')
        b, wb = await join(c2, m2, "r", "bob")
        assert [p["name"] for p in wb["room"]["participants"]] == ["bob"]
        assert "ghost" not in await m1.broker.hgetall("presence:r")
        await b.close()

    run(scenario())
