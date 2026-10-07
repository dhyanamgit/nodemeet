"""The easy layer: plain-string config, one-line helpers, friendly errors."""
import json
import os
import tempfile

import pytest
from asgi_client import ASGIClient
from fakes import FakeSFU
from helpers import run
from nodemeet import NodeMeet
from nodemeet.easy import (friendly_permissions, mailer_from_url, parse_hours, parse_minutes,
                           parse_price, room_settings, storage_from_url)
from nodemeet.storage import DocumentStorage, KeyValueStorage, MemoryStorage, SQLiteStorage

SECRET = "easy-layer-secret-long-enough!!"


def make(**kw):
    kw.setdefault("secret", SECRET)
    return NodeMeet(reminders=False, sfu=FakeSFU(), base_url="http://test", **kw)


def test_no_arguments_needed():
    meet = NodeMeet(reminders=False, sfu=False)
    assert meet.secret_generated and isinstance(meet.storage, MemoryStorage)
    assert meet.room("x").host_link().startswith(meet.base_url + "/r/x#token=")


def test_parsers():
    assert parse_hours("mon-fri 9-17") == {"mon-fri": "09:00-17:00"}
    assert parse_hours("mon-fri 9am-12pm, 2pm-6pm; sat 10-13") == {
        "mon-fri": "09:00-12:00, 14:00-18:00", "sat": "10:00-13:00"}
    assert parse_hours("weekdays 9:30 to 17:00") == {"mon-fri": "09:30-17:00"}
    assert parse_minutes("2h") == 120 and parse_minutes("1d") == 1440 and parse_minutes(15) == 15
    assert parse_price("₹499") == (49900, "INR") and parse_price("$19.99") == (1999, "USD")
    assert parse_price(500, "INR") == (500, "INR")  # ints stay in the smallest unit
    with pytest.raises(ValueError):
        parse_hours("someday 9-5")


def test_storage_and_mailer_urls():
    d = tempfile.mkdtemp()
    assert isinstance(storage_from_url(None), MemoryStorage)
    assert isinstance(storage_from_url(os.path.join(d, "a.db")), SQLiteStorage)
    assert isinstance(storage_from_url(f"sqlite:///{d}/b.db"), SQLiteStorage)
    assert isinstance(storage_from_url(f"dir:///{d}/data"), KeyValueStorage)
    assert isinstance(storage_from_url("couchdb://u:p@localhost:5984"), DocumentStorage)
    with pytest.raises(ValueError) as e:
        storage_from_url("mongo-db://x")
    assert "mongodb" in str(e.value)
    with pytest.raises(ImportError) as e:  # not installed here: the message says what to install
        storage_from_url("mongodb://localhost/app")
    assert 'pip install "nodemeet[mongo]"' in str(e.value)
    m = mailer_from_url("smtp://me@gmail.com:app pw@smtp.gmail.com:587?name=Acme")
    assert (m.host, m.port, m.username, m.password, m.sender, m.security) == (
        "smtp.gmail.com", 587, "me@gmail.com", "app pw", "me@gmail.com", "starttls")
    assert mailer_from_url("smtps://u:p@mail.x.com").port == 465


def test_friendly_errors():
    meet = make()
    with pytest.raises(ValueError) as e:
        meet.room("r", preset="webnar")
    assert "webinar" in str(e.value)
    with pytest.raises(TypeError) as e:
        meet.room("r", waitng_room=False)
    assert "waiting_room" in str(e.value)
    with pytest.raises(ValueError) as e:
        meet.role("x", can="moderate.kik")
    assert "moderate.kick" in str(e.value)
    with pytest.raises(ValueError) as e:
        meet.on("jion", lambda *a: None)
    assert "join" in str(e.value)
    with pytest.raises(ValueError) as e:
        meet.hours("ada", timezone="Asia/Kolkatta")
    assert "Asia/Kolkata" in str(e.value)
    with pytest.raises(TypeError) as e:
        meet.brand(colour="#fff")
    assert "color" in str(e.value)
    with pytest.raises(ValueError) as e:
        meet.add_payments("stripe")  # no keys, no env vars
    assert "STRIPE_SECRET_KEY" in str(e.value)


def test_friendly_permission_words():
    assert friendly_permissions("mic, camera") == ["audio.publish", "audio.unmute_self",
                                                   "video.publish", "video.start_self"]
    assert room_settings("webinar", max=10)["max_participants"] == 10


def test_declared_rooms_roles_hours_and_brand():
    meet = make()
    meet.room("class", preset="classroom", join_list="a@x.com, b@x.com")
    meet.room("hall", preset="open", name="Town hall")
    meet.role("teacher", can="moderate, record", badge="T", rank=50)
    meet.role("student", cannot="screen, dm")
    meet.role("viewer", can="chat")
    meet.hours("ada", "mon-fri 9-17", timezone="Asia/Kolkata", minutes="45m", buffer=10, price="499 INR")
    meet.brand(name="Acme", color="#ff5a00", whiteboard=False)

    async def scenario():
        await meet.startup()
        cls = await meet.storage.get_room("class")
        assert cls.lobby and cls.allowed_users == ["a@x.com", "b@x.com"]
        assert "screen.publish" in cls.role_overrides["participant"]["revoke"]
        assert not (await meet.storage.get_room("hall")).lobby
        teacher = await meet.roles.get("teacher")
        assert {"moderate.kick", "recording.start", "audio.publish"} <= teacher.permissions
        assert teacher.attributes["rank"] == 50
        student = await meet.roles.get("student")
        assert "screen.publish" not in student.permissions and "audio.publish" in student.permissions
        viewer = await meet.roles.get("viewer")
        assert "chat.send" in viewer.permissions and "audio.publish" not in viewer.permissions
        av = await meet.storage.get_availability("ada")
        assert (av.timezone, av.duration_minutes, av.buffer_after, av.price, av.currency) == (
            "Asia/Kolkata", 45, 10, 49900, "INR")
        b = await meet.branding.resolve()
        assert b["name"] == "Acme" and b["colors"]["primary"] == "#ff5a00" and not b["features"]["whiteboard"]
        await meet.shutdown()

    run(scenario())


def test_declarations_dont_clobber_admin_edits_until_code_changes():
    path = os.path.join(tempfile.mkdtemp(), "m.db")

    async def boot(max_people):
        meet = make(db=path)
        meet.room("r", max_participants=max_people)
        await meet.startup()
        return meet

    async def scenario():
        meet = await boot(10)
        cfg = await meet.storage.get_room("r")
        cfg.locked = True                     # an admin changes something at runtime
        cfg.max_participants = 3
        await meet.rooms.update_config(cfg)
        await meet.shutdown()
        meet = await boot(10)                 # same code: the admin's edit survives a restart
        assert (await meet.storage.get_room("r")).max_participants == 3
        await meet.shutdown()
        meet = await boot(25)                 # code changed: the new value wins
        cfg = await meet.storage.get_room("r")
        assert cfg.max_participants == 25 and cfg.locked
        await meet.shutdown()

    run(scenario())


def test_links_and_room_handles_work_over_the_wire():
    meet = make()
    open_room = meet.room("open", preset="open")
    meet.room("guarded")

    async def scenario():
        c = ASGIClient(meet.asgi())

        async def join(url):
            token = url.split("#token=", 1)[1]
            ws = await c.ws("/ws")
            await ws.send_json({"type": "join", "token": token})
            return ws

        guest = await join(open_room.guest_link("Bob"))
        w = await guest.recv_until("welcome")
        assert w["you"]["name"] == "Bob"
        waiting = await join(meet.guest_link("guarded", "Eve"))
        await waiting.recv_until("lobby")
        skip = await join(meet.link("guarded", "Dee", skip_waiting_room=True, cannot="chat"))
        welcome = await skip.recv_until("welcome")
        assert "chat.send" not in welcome["permissions"] and "audio.publish" in welcome["permissions"]
        for ws in (guest, waiting, skip):
            await ws.close()
        await meet.shutdown()

    run(scenario())


def test_slots_book_reschedule_cancel_shortcuts():
    meet = make(email="memory")
    page = meet.hours("ada", "mon-sun 0-23:59", minutes=30)
    booked = []
    meet.on("booked", lambda b: booked.append(b))

    async def scenario():
        await meet.startup()
        slots = await page.slots(days=3)
        assert slots
        b = await meet.book("ada", slots[2], name="Bob", email="bob@x.com")
        assert booked and booked[0].id == b.id
        b2 = await meet.reschedule(b, slots[5].start.isoformat())
        assert b2.start == slots[5].start
        assert (await meet.cancel(b2)).status.value == "cancelled"
        assert meet.bookings.mailer.outbox
        await meet.shutdown()

    assert page.url == "http://test/book/ada"
    run(scenario())


def test_config_file_and_env():
    d = tempfile.mkdtemp()
    os.environ["NM_TEST_SECRET"] = SECRET
    cfg = {"secret": "${NM_TEST_SECRET}", "db": f"{d}/c.db", "email": "memory",
           "base_url": "${NM_TEST_URL:-http://cfg}", "sfu": False, "reminders": False,
           "brand": {"name": "Cfg"}, "rooms": {"standup": {"preset": "open"}},
           "hours": {"ada": {"spec": "mon-fri 9-17", "timezone": "UTC"}},
           "roles": {"mod": {"can": ["moderate"], "rank": 40}}}
    with open(f"{d}/nodemeet.json", "w") as f:
        json.dump(cfg, f)
    meet = NodeMeet.from_config(f"{d}/nodemeet.json")
    assert meet.base_url == "http://cfg" and not meet.secret_generated

    async def scenario():
        await meet.startup()
        assert not (await meet.storage.get_room("standup")).lobby
        assert (await meet.roles.get("mod")).rank == 40
        assert (await meet.branding.resolve())["name"] == "Cfg"
        await meet.shutdown()

    run(scenario())
    with open(f"{d}/bad.json", "w") as f:
        json.dump({"secrett": "x"}, f)
    with pytest.raises(TypeError) as e:
        NodeMeet.from_config(f"{d}/bad.json")
    assert "secret" in str(e.value)
    with open(f"{d}/.env", "w") as f:
        f.write(f"NODEMEET_SECRET={SECRET}\nNODEMEET_WAITING_ROOM=false\n")
    old = {k: os.environ.pop(k, None) for k in ("NODEMEET_SECRET", "NODEMEET_WAITING_ROOM")}
    try:
        meet = NodeMeet.from_env(dotenv=f"{d}/.env", sfu=False, reminders=False)
        assert not meet.secret_generated and meet.waiting_room is False
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_cli_init_and_link():
    from nodemeet.cli import main
    d = tempfile.mkdtemp()
    assert main(["init", d]) == 0
    assert {"app.py", "nodemeet.toml", ".env", ".gitignore"} <= set(os.listdir(d))
    cwd = os.getcwd()
    old = os.environ.pop("NODEMEET_SECRET", None)
    try:
        os.chdir(d)
        meet = NodeMeet.from_config("nodemeet.toml", sfu=False, reminders=False)
        assert not meet.secret_generated  # secret came from the generated .env
        assert main(["link", "standup", "Ada", "--host"]) == 0
    finally:
        os.chdir(cwd)
        os.environ.pop("NODEMEET_SECRET", None)
        if old is not None:
            os.environ["NODEMEET_SECRET"] = old
