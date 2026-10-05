import pytest

from helpers import run
from nodemeet import Hooks, JoinRejected, MemoryStorage, Participant, Role, RoomManager, select_topology
from nodemeet.exceptions import RoomFull, RoomNotFound
from nodemeet.hooks import JoinContext
from nodemeet.models import RoomConfig
from nodemeet.tokens import TokenSigner


def test_topology_rules():
    assert select_topology("auto", 4) == "p2p"
    assert select_topology("auto", 5) == "sfu"
    assert select_topology("auto", 3, "sfu") == "sfu"  # hysteresis
    assert select_topology("auto", 2, "sfu") == "p2p"
    assert select_topology("auto", 50, sfu_available=False) == "p2p"
    assert select_topology("webinar", 1) == "sfu"
    assert select_topology("p2p", 30) == "p2p"
    with pytest.raises(ValueError):
        select_topology("bogus", 1)


def test_room_manager_switches_topology_and_closes():
    closed = []

    async def scenario():
        hooks = Hooks()
        hooks.on("on_room_closed", lambda room: closed.append(room.id))
        mgr = RoomManager(MemoryStorage(), hooks, p2p_max=4, sfu_available=True)
        room = await mgr.open("r1")
        people = [Participant(user_id=f"u{i}", name=f"U{i}", role=Role.PARTICIPANT) for i in range(6)]
        changes = [await mgr.add(room, p) for p in people]
        assert changes == [False, False, False, False, True, False] and room.topology == "sfu"
        for p in people:
            await mgr.remove(room, p.peer_id)
        assert closed == ["r1"] and mgr.get("r1") is None

    run(scenario())


def test_capacity_autocreate_and_webinar_publishing():
    async def scenario():
        storage = MemoryStorage()
        mgr = RoomManager(storage, Hooks(), auto_create=False)
        with pytest.raises(RoomNotFound):
            await mgr.open("nope")
        await mgr.create(RoomConfig(id="web", mode="webinar", max_participants=2))
        room = await mgr.open("web")
        host = Participant(user_id="h", name="H", role=Role.HOST)
        guest = Participant(user_id="g", name="G", role=Role.PARTICIPANT)
        await mgr.add(room, host)
        await mgr.add(room, guest)
        assert room.can_publish(host) and not room.can_publish(guest)
        guest.promoted = True
        assert room.can_publish(guest)
        with pytest.raises(RoomFull):
            mgr.check_capacity(room)

    run(scenario())


def test_hooks_before_join_and_chat_filters():
    hooks = Hooks()
    claims = TokenSigner("x" * 20).verify(TokenSigner("x" * 20).create("r", "u"))
    ctx = JoinContext(room_id="r", claims=claims, name="U", role=claims.role)

    @hooks.before_join
    def only_named(c):
        if c.name == "banned":
            raise JoinRejected("nope")
        c.name = c.name.upper()

    hooks.on("on_chat", lambda room, p, text: False if "spam" in text else text.replace("darn", "****"))

    async def scenario():
        await hooks.run_before_join(ctx)
        assert ctx.name == "U"
        ctx.name = "banned"
        with pytest.raises(JoinRejected):
            await hooks.run_before_join(ctx)
        assert await hooks.run_on_chat(None, None, "buy spam") is None
        assert await hooks.run_on_chat(None, None, "darn it") == "**** it"
        hooks.on("on_join", lambda *a: 1 / 0)
        await hooks.emit("on_join", None, None)  # errors are logged, not raised

    run(scenario())
    with pytest.raises(ValueError):
        hooks.on("on_nothing", print)
