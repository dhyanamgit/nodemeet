"""Polls, Q&A, whiteboard, breakout rooms, recording, captions, lobby extras, device bans."""
import tempfile

from asgi_client import ASGIClient
from fakes import FakeSFU
from helpers import run
from nodemeet import MemoryMailer, NodeMeet
from nodemeet.recordings import FileRecordingStore

SECRET = "collab-secret-long-enough-123"


def make(**kw):
    kw.setdefault("waiting_room", False)
    return NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=FakeSFU(),
                    base_url="http://test", **kw)


async def connect(c, meet, room, user, role="participant", device=None, **tok):
    ws = await c.ws("/ws")
    await ws.send_json({"type": "join", "token": meet.create_token(room, user, role, name=user, **tok),
                        "device": device})
    return ws


def test_polls_respect_results_permission_and_anonymity():
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "r", "ada", "host"); await host.recv_until("features")
        bob = await connect(c, meet, "r", "bob"); await bob.recv_until("features")
        await host.send_json({"type": "poll-create", "question": "Lunch?", "options": ["Pizza", "Sushi"]})
        hp = (await host.recv_until("poll"))["poll"]
        bp = (await bob.recv_until("poll"))["poll"]
        assert "results" in hp and "results" not in bp          # live results only for hosts
        await bob.send_json({"type": "poll-vote", "id": hp["id"], "choices": ["o1"]})
        assert (await host.recv_until("poll"))["poll"]["results"] == {"o0": 0, "o1": 1}
        await bob.send_json({"type": "poll-create", "question": "x", "options": ["a", "b"]})
        assert (await bob.recv_until("error"))["code"] == "forbidden"
        await host.send_json({"type": "poll-close", "id": hp["id"]})
        closed = (await bob.recv_until("poll"))["poll"]
        assert closed["open"] is False and closed["results"]["o1"] == 1 and "voters" not in closed
        assert await meet.storage.get_record("poll", f"r|{hp['id']}")
        late = await connect(c, meet, "r", "cat")
        feats = await late.recv_until("features")
        assert feats["polls"][0]["results"]["o1"] == 1            # late joiners get the state
        for ws in (host, bob, late):
            await ws.close()

    run(scenario())


def test_qa_and_whiteboard():
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "r", "ada", "host"); await host.recv_until("features")
        bob = await connect(c, meet, "r", "bob"); await bob.recv_until("features")
        await bob.send_json({"type": "qa-ask", "text": "When is the deadline?", "anonymous": True})
        q = (await host.recv_until("question"))["question"]
        assert q["by"] == "Anonymous" and "user_id" not in q and q["votes"] == 0
        await bob.recv_until("question")
        await host.send_json({"type": "qa-upvote", "id": q["id"]})
        assert (await bob.recv_until("question"))["question"]["votes"] == 1
        await host.send_json({"type": "qa-answer", "id": q["id"], "text": "Friday", "answered": True})
        ans = (await bob.recv_until("question"))["question"]
        assert ans["answered"] and ans["answer"]["text"] == "Friday"
        await bob.send_json({"type": "qa-answer", "id": q["id"], "dismiss": True})
        assert (await bob.recv_until("error"))["code"] == "forbidden"
        await bob.send_json({"type": "board-draw", "stroke": {"points": [[0.1, 0.1], [0.5, 0.5]], "color": "#f00"}})
        s = (await host.recv_until("board"))["stroke"]
        assert s["points"][1] == [0.5, 0.5] and s["by"] == "bob"
        await bob.send_json({"type": "board-clear"})
        assert (await bob.recv_until("error"))["code"] == "forbidden"
        await host.send_json({"type": "board-clear"})
        assert (await bob.recv_until("board"))["op"] == "clear"
        await host.close(); await bob.close()

    run(scenario())


def test_breakout_rooms_assign_and_return():
    meet = make()

    async def scenario():
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "main", "ada", "host"); await host.recv_until("features")
        bob = await connect(c, meet, "main", "bob"); wb = await bob.recv_until("welcome")
        await host.send_json({"type": "breakout", "action": "set", "rooms": [{"name": "Team A", "members": [wb["peer_id"]]}]})
        state = (await host.recv_until("breakouts"))["breakouts"]
        assert state["status"] == "draft" and state["assignments"]["main~1"] == ["bob"]
        await host.send_json({"type": "breakout", "action": "open"})
        assign = await bob.recv_until("breakout-assign")
        assert assign["room"] == "main~1" and assign["name"] == "Team A"
        await bob.close()
        sub = await c.ws("/ws")
        await sub.send_json({"type": "join", "token": assign["token"]})   # skips the waiting room
        assert (await sub.recv_until("welcome"))["room"]["id"] == "main~1"
        await host.send_json({"type": "breakout", "action": "message", "text": "5 minutes left"})
        assert (await sub.recv_until("announcement"))["text"] == "5 minutes left"
        await host.send_json({"type": "breakout", "action": "close", "seconds": 0})
        back = await sub.recv_until("breakout-return")
        assert back["room"] == "main" and meet.tokens.verify(back["token"]).room == "main"
        await sub.close(); await host.close()

    run(scenario())


def test_recording_upload_and_download():
    root = tempfile.mkdtemp()
    meet = make(recording_store=FileRecordingStore(root))

    async def scenario():
        c = ASGIClient(meet.asgi())
        tok = meet.create_token("r", "ada", "host", name="ada")
        host = await c.ws("/ws"); await host.send_json({"type": "join", "token": tok}); await host.recv_until("features")
        bob = await connect(c, meet, "r", "bob"); await bob.recv_until("features")
        await bob.send_json({"type": "recording", "action": "start"})
        assert (await bob.recv_until("error"))["code"] == "forbidden"
        await host.send_json({"type": "recording", "action": "start"})
        rec = (await bob.recv_until("recording"))["recording"]   # everyone sees REC
        assert rec["by"] == "ada"
        h = {"X-Join-Token": tok}
        r1 = await c.request("POST", f"/api/recordings/{rec['id']}/chunks?seq=0", headers=h, body=b"AAA")
        assert r1.status == 200
        bob_tok = meet.create_token("r", "bob")
        r = await c.request("POST", f"/api/recordings/{rec['id']}/chunks?seq=1", headers={"X-Join-Token": bob_tok}, body=b"x")
        assert r.status == 403
        await host.send_json({"type": "recording", "action": "stop"})
        assert (await bob.recv_until("recording"))["recording"] is None
        r2 = await c.request("POST", f"/api/recordings/{rec['id']}/chunks?seq=1&final=1", headers=h, body=b"BBB")
        assert r2.json()["status"] == "ready"
        d = await c.request("GET", f"/api/recordings/{rec['id']}/download", headers={"Authorization": "Bearer k"})
        assert d.status == 200 and d.body == b"AAABBB"
        d = await c.request("GET", f"/api/recordings/{rec['id']}/download?token={bob_tok}")
        assert d.status == 403                                   # participants lack recording.download
        lst = await c.request("GET", "/api/recordings?room=r", headers={"Authorization": "Bearer k"})
        assert lst.json()["recordings"][0]["size"] == 6
        await host.close(); await bob.close()

    run(scenario())


class FakeTranscriber:
    async def transcribe(self, audio, mime="audio/webm", language=None):
        return f"heard {len(audio)} bytes"


def test_captions_browser_and_server_engines_and_transcript():
    meet = make(transcriber=FakeTranscriber())

    async def scenario():
        c = ASGIClient(meet.asgi())
        host = await connect(c, meet, "r", "ada", "host"); await host.recv_until("features")
        bob_tok = meet.create_token("r", "bob", name="bob")
        bob = await c.ws("/ws"); await bob.send_json({"type": "join", "token": bob_tok}); await bob.recv_until("features")
        await host.send_json({"type": "captions", "enabled": True, "engine": "browser"})
        assert (await bob.recv_until("captions-state"))["captions"]["enabled"]
        await bob.send_json({"type": "caption-text", "text": "hello everyone", "final": True})
        cap = await host.recv_until("caption")
        assert cap["name"] == "bob" and cap["text"] == "hello everyone"
        r = await c.request("POST", "/api/rooms/r/captions/audio", headers={"X-Join-Token": bob_tok,
                                                                           "Content-Type": "audio/webm"}, body=b"1234")
        assert r.json()["text"] == "heard 4 bytes"
        txt = await c.request("GET", "/api/rooms/r/transcript?format=txt", headers={"Authorization": "Bearer k"})
        assert "bob: hello everyone" in txt.text and "bob: heard 4 bytes" in txt.text
        vtt = await c.request("GET", "/api/rooms/r/transcript?format=vtt", headers={"Authorization": "Bearer k"})
        assert vtt.text.startswith("WEBVTT") and "<v bob>" in vtt.text
        await host.close(); await bob.close()

    run(scenario())


def test_lobby_until_host_message_position_and_device_bans():
    meet = make(waiting_room=True)

    async def scenario():
        await meet.create_room(room_id="r")
        cfg = await meet.storage.get_room("r")
        cfg.lobby_mode = "until_host"
        await meet.rooms.update_config(cfg)
        c = ASGIClient(meet.asgi())
        bob = await connect(c, meet, "r", "bob", device="dev_bob")
        lob = await bob.recv_until("lobby")
        assert lob["mode"] == "until_host"
        assert (await bob.recv_until("lobby-position"))["position"] == 1
        host = await connect(c, meet, "r", "ada", "host"); await host.recv_until("welcome")
        await bob.recv_until("admitted")                       # host arrived: everyone in
        wb = await bob.recv_until("welcome")
        await host.send_json({"type": "moderate", "action": "lobby-message", "text": "Starting at 10"})
        assert (await host.recv_until("room-updated"))["room"]["lobby_message"] == "Starting at 10"
        await host.send_json({"type": "moderate", "action": "ban", "target": wb["peer_id"]})
        await bob.recv_until("banned")
        cfg = await meet.storage.get_room("r")
        assert "dev_bob" in cfg.banned_devices
        sneaky = await connect(c, meet, "r", "bob-new-account", device="dev_bob")   # new account, same browser
        assert (await sneaky.recv_until("error"))["code"] == "banned"
        await host.send_json({"type": "moderate", "action": "unban", "user_id": "bob"})
        while (await host.recv_until("room-updated"))["room"].get("banned"):
            pass
        assert "dev_bob" not in (await meet.storage.get_room("r")).banned_devices
        await host.close()

    run(scenario())
