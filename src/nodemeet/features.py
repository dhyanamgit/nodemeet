"""In-meeting collaboration: polls, Q&A, whiteboard, breakout rooms, recording and
caption state. Mixed into the signaling ``Session``.

Every state change is broadcast; servers in a cluster apply the same messages to
their mirror of the room (see :func:`apply_remote`), so late joiners on any
server get the full state in the ``features`` message sent right after ``welcome``.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .rooms import Participant, Room
    from .server import NodeMeet

MAX_OPTIONS = 20
MAX_STROKES = 5000
MAX_POINTS = 2000


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(5)


# -- public shapes ---------------------------------------------------------------
def poll_public(poll: Dict[str, Any], with_results: bool) -> Dict[str, Any]:
    out = {k: poll[k] for k in ("id", "question", "options", "multiple", "anonymous", "open",
                                "by", "created_at")}
    if with_results or not poll["open"]:
        out["results"] = poll_counts(poll)
        if not poll["anonymous"]:
            out["voters"] = {opt["id"]: [poll["names"].get(u, u) for u, ch in poll["votes"].items()
                                         if opt["id"] in ch] for opt in poll["options"]}
    out["total_votes"] = len(poll["votes"])
    return out


def poll_counts(poll: Dict[str, Any]) -> Dict[str, int]:
    counts = {o["id"]: 0 for o in poll["options"]}
    for choices in poll["votes"].values():
        for c in choices:
            if c in counts:
                counts[c] += 1
    return counts


def question_public(q: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in q.items() if k not in ("voters", "user_id")}
    out["votes"] = len(q["voters"])
    return out


def feature_state(room: "Room", me: "Participant") -> Dict[str, Any]:
    can = me.can
    return {
        "type": "features",
        "polls": [poll_public(p, can("polls.results")) for p in room.polls.values()],
        "my_votes": {pid: p["votes"].get(me.user_id, []) for pid, p in room.polls.items()},
        "questions": [question_public(q) for q in room.questions.values()],
        "board": room.board if can("whiteboard.view") else [],
        "breakouts": breakout_public(room, manager=can("breakout.manage")),
        "recording": room.recording,
        "captions": room.captions,
    }


def breakout_public(room: "Room", manager: bool) -> Dict[str, Any]:
    b = room.breakouts or {}
    if not b:
        return {}
    rooms = [{"id": r["id"], "name": r["name"], "count": len(r.get("members", []))}
             for r in b.get("rooms", [])]
    out = {"status": b.get("status"), "rooms": rooms, "allow_choose": b.get("allow_choose", False),
           "ends_at": b.get("ends_at")}
    if manager:
        out["assignments"] = {r["id"]: r.get("members", []) for r in b.get("rooms", [])}
    return out


def apply_remote(room: "Room", m: Dict[str, Any]) -> None:
    """Keep another server's mirror of the room in sync (called by the cluster bus)."""
    kind = m.get("type")
    if kind == "_poll":
        room.polls[m["poll"]["id"]] = m["poll"]
    elif kind == "_poll-delete":
        room.polls.pop(m.get("id"), None)
    elif kind == "_question":
        room.questions[m["question"]["id"]] = m["question"]
    elif kind == "_question-delete":
        room.questions.pop(m.get("id"), None)
    elif kind == "board":
        _board_apply(room, m)
    elif kind == "recording":
        room.recording = m.get("recording")
    elif kind == "captions-state":
        room.captions = m.get("captions") or {"enabled": False}
    elif kind == "_breakouts":
        room.breakouts = m.get("state") or {}


def _board_apply(room: "Room", m: Dict[str, Any]) -> None:
    op = m.get("op")
    if op == "add":
        room.board.append(m["stroke"])
        del room.board[:-MAX_STROKES]
    elif op == "remove":
        room.board[:] = [s for s in room.board if s.get("id") != m.get("id")]
    elif op == "clear":
        room.board.clear()


class FeaturesMixin:
    """Handlers for collaboration messages. ``self`` is a signaling Session."""

    meet: "NodeMeet"
    room: Optional["Room"]
    me: Optional["Participant"]

    async def _sync(self, message: Dict[str, Any]) -> None:
        """Internal state op: other servers apply it; clients never see it."""
        assert self.room
        await self.room.broadcast(message)

    async def send_features(self) -> None:
        assert self.room and self.me
        await self.send(feature_state(self.room, self.me))  # type: ignore[attr-defined]

    # -- polls ---------------------------------------------------------------------
    async def _poll_changed(self, poll: Dict[str, Any]) -> None:
        room = self.room
        assert room
        await self._sync({"type": "_poll", "poll": poll})
        await room.broadcast({"type": "poll", "poll": poll_public(poll, False), "full": False})
        await room.broadcast({"type": "poll", "poll": poll_public(poll, True), "full": True})

    async def on_poll_create(self, data: Dict[str, Any]) -> None:
        me = self.me
        assert me and self.room
        if not me.can("polls.create"):
            return await self.forbid("polls.create")  # type: ignore[attr-defined]
        question = str(data.get("question", "")).strip()[:300]
        options = [str(o).strip()[:120] for o in (data.get("options") or []) if str(o).strip()]
        if not question or not 2 <= len(options) <= MAX_OPTIONS:
            return await self.error("bad_request", "a poll needs a question and 2-20 options")  # type: ignore[attr-defined]
        poll = {"id": _id("poll_"), "question": question,
                "options": [{"id": f"o{i}", "text": t} for i, t in enumerate(options)],
                "multiple": bool(data.get("multiple")), "anonymous": data.get("anonymous", True) is not False,
                "open": True, "by": me.name, "created_at": time.time(), "votes": {}, "names": {}}
        self.room.polls[poll["id"]] = poll
        await self._poll_changed(poll)

    async def on_poll_vote(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("polls.vote"):
            return await self.forbid("polls.vote")  # type: ignore[attr-defined]
        poll = room.polls.get(str(data.get("id", "")))
        if poll is None or not poll["open"]:
            return await self.error("poll_closed", "this poll is closed")  # type: ignore[attr-defined]
        valid = {o["id"] for o in poll["options"]}
        choices = [c for c in dict.fromkeys(data.get("choices") or []) if c in valid]
        if not choices or (len(choices) > 1 and not poll["multiple"]):
            return await self.error("bad_request", "pick one option" if not poll["multiple"] else "pick an option")  # type: ignore[attr-defined]
        poll["votes"][me.user_id] = choices
        poll["names"][me.user_id] = me.name
        await self._poll_changed(poll)

    async def on_poll_close(self, data: Dict[str, Any]) -> None:
        await self._poll_admin(data, close=True)

    async def on_poll_delete(self, data: Dict[str, Any]) -> None:
        await self._poll_admin(data, close=False)

    async def _poll_admin(self, data: Dict[str, Any], *, close: bool) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("polls.create"):
            return await self.forbid("polls.create")  # type: ignore[attr-defined]
        poll = room.polls.get(str(data.get("id", "")))
        if poll is None:
            return
        if close:
            poll["open"] = False
            await self.meet.storage.put_record("poll", f"{room.id}|{poll['id']}", poll)
            await self._poll_changed(poll)
            await self.meet.emit_webhook("poll.closed", {"room": room.id, **poll_public(poll, True)},
                                         tenant_id=room.config.tenant_id)
        else:
            room.polls.pop(poll["id"], None)
            await self._sync({"type": "_poll-delete", "id": poll["id"]})
            await room.broadcast({"type": "poll-removed", "id": poll["id"]})

    # -- Q&A -------------------------------------------------------------------------
    async def _question_changed(self, q: Dict[str, Any]) -> None:
        assert self.room
        await self._sync({"type": "_question", "question": q})
        await self.room.broadcast({"type": "question", "question": question_public(q)})

    async def on_qa_ask(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("qa.ask"):
            return await self.forbid("qa.ask")  # type: ignore[attr-defined]
        text = str(data.get("text", "")).strip()[:1000]
        if not text:
            return
        anon = bool(data.get("anonymous"))
        q = {"id": _id("q_"), "text": text, "by": "Anonymous" if anon else me.name,
             "peer_id": None if anon else me.peer_id, "user_id": me.user_id, "voters": [],
             "answered": False, "highlighted": False, "answer": None, "ts": time.time()}
        room.questions[q["id"]] = q
        await self._question_changed(q)

    async def on_qa_upvote(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("qa.upvote"):
            return await self.forbid("qa.upvote")  # type: ignore[attr-defined]
        q = room.questions.get(str(data.get("id", "")))
        if q is None:
            return
        if me.user_id in q["voters"]:
            q["voters"].remove(me.user_id)
        else:
            q["voters"].append(me.user_id)
        await self._question_changed(q)

    async def on_qa_answer(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("qa.answer"):
            return await self.forbid("qa.answer")  # type: ignore[attr-defined]
        q = room.questions.get(str(data.get("id", "")))
        if q is None:
            return
        if data.get("dismiss"):
            room.questions.pop(q["id"], None)
            await self._sync({"type": "_question-delete", "id": q["id"]})
            await room.broadcast({"type": "question-removed", "id": q["id"]})
            return
        if "text" in data:
            q["answer"] = {"text": str(data["text"])[:2000], "by": me.name}
        if "answered" in data:
            q["answered"] = bool(data["answered"])
        if "highlighted" in data:
            for other in room.questions.values():
                other["highlighted"] = False
            q["highlighted"] = bool(data["highlighted"])
        await self._question_changed(q)

    # -- whiteboard ----------------------------------------------------------------------
    async def on_board_draw(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("whiteboard.draw"):
            return await self.forbid("whiteboard.draw")  # type: ignore[attr-defined]
        s = data.get("stroke") or {}
        try:
            points = [[round(float(x), 4), round(float(y), 4)] for x, y in (s.get("points") or [])][:MAX_POINTS]
        except (TypeError, ValueError):
            return await self.error("bad_request", "points must be [[x, y], ...] between 0 and 1")  # type: ignore[attr-defined]
        tool = s.get("tool") if s.get("tool") in ("pen", "highlighter", "eraser", "line", "rect", "text") else "pen"
        stroke = {"id": _id("s_"), "points": points, "tool": tool,
                  "color": str(s.get("color") or "#111111")[:20], "width": max(1, min(40, int(s.get("width") or 3))),
                  "text": str(s.get("text") or "")[:200], "by": me.user_id, "name": me.name}
        if not points:
            return
        msg = {"type": "board", "op": "add", "stroke": stroke}
        _board_apply(room, msg)
        await room.broadcast(msg)

    async def on_board_undo(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        sid = data.get("id")
        if not sid:  # undo my last stroke
            mine = [s for s in room.board if s.get("by") == me.user_id]
            sid = mine[-1]["id"] if mine else None
        stroke = next((s for s in room.board if s.get("id") == sid), None)
        if stroke is None:
            return
        if stroke.get("by") != me.user_id and not me.can("whiteboard.clear"):
            return await self.forbid("whiteboard.clear")  # type: ignore[attr-defined]
        msg = {"type": "board", "op": "remove", "id": sid}
        _board_apply(room, msg)
        await room.broadcast(msg)

    async def on_board_clear(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("whiteboard.clear"):
            return await self.forbid("whiteboard.clear")  # type: ignore[attr-defined]
        msg = {"type": "board", "op": "clear", "by": me.name}
        _board_apply(room, msg)
        await room.broadcast(msg)

    # -- recording state (media upload goes over HTTP, see recordings.py) ------------------
    async def on_recording(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("recording.start"):
            return await self.forbid("recording.start")  # type: ignore[attr-defined]
        action = data.get("action")
        if action == "start":
            if room.recording:
                return await self.error("already_recording", "this meeting is already being recorded")  # type: ignore[attr-defined]
            rec = await self.meet.recordings.begin(room, me)
            room.recording = {"id": rec["id"], "by": me.name, "by_peer": me.peer_id,
                              "started_at": rec["started_at"]}
        elif action == "stop":
            if not room.recording:
                return
            rec_id = room.recording["id"]
            room.recording = None
            await self.meet.recordings.stop(rec_id)
        else:
            return await self.error("bad_request", "action must be start or stop")  # type: ignore[attr-defined]
        await room.broadcast({"type": "recording", "recording": room.recording})

    # -- captions -----------------------------------------------------------------------------
    async def on_captions(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        if not me.can("captions.enable"):
            return await self.forbid("captions.enable")  # type: ignore[attr-defined]
        engine = data.get("engine") or ("server" if self.meet.transcriber else "browser")
        if engine == "server" and not self.meet.transcriber:
            return await self.error("no_transcriber", "server captions need NodeMeet(transcriber=...)")  # type: ignore[attr-defined]
        room.captions = {"enabled": bool(data.get("enabled", True)), "engine": engine,
                         "language": str(data.get("language") or "")[:10] or None, "by": me.name}
        await room.broadcast({"type": "captions-state", "captions": room.captions})

    async def on_caption_text(self, data: Dict[str, Any]) -> None:
        """Captions produced in the browser (Web Speech API); the server relays + stores them."""
        me, room = self.me, self.room
        assert me and room
        if not room.captions.get("enabled"):
            return
        text = str(data.get("text", "")).strip()[:500]
        if text:
            await self.meet.captions.publish(room, me, text, final=bool(data.get("final", True)))

    # -- breakout rooms -------------------------------------------------------------------
    async def on_breakout(self, data: Dict[str, Any]) -> None:
        me, room = self.me, self.room
        assert me and room
        action = str(data.get("action", ""))
        if action == "choose":
            return await self._breakout_choose(str(data.get("room", "")))
        if not me.can("breakout.manage"):
            return await self.forbid("breakout.manage")  # type: ignore[attr-defined]
        mgr = self.meet.breakouts
        if action == "set":
            await mgr.configure(room, data, by=me.name)
        elif action == "open":
            await mgr.open(room)
        elif action == "close":
            asyncio.ensure_future(mgr.close(room, seconds=int(data.get("seconds", 30) or 0)))
        elif action == "message":
            await mgr.announce(room, str(data.get("text", ""))[:500], by=me.name)
        elif action == "move":
            await mgr.move(room, str(data.get("user_id", "")), str(data.get("room", "")))
        elif action == "join":  # managers hop between rooms
            await mgr.send_pass(room, me, str(data.get("room", "")))
        else:
            await self.error("bad_request", "unknown breakout action")  # type: ignore[attr-defined]

    async def _breakout_choose(self, sub_id: str) -> None:
        me, room = self.me, self.room
        assert me and room
        b = room.breakouts or {}
        if b.get("status") != "open" or not (b.get("allow_choose") and me.can("breakout.choose")):
            return await self.forbid("breakout.choose")  # type: ignore[attr-defined]
        await self.meet.breakouts.move(room, me.user_id, sub_id)


class BreakoutManager:
    """Breakout rooms are ordinary rooms named ``<main>~<n>``; people are moved with
    freshly minted join tokens, so every permission and branding rule still applies."""

    def __init__(self, meet: "NodeMeet") -> None:
        self.meet = meet
        self._timers: Dict[str, asyncio.Task] = {}

    async def _publish(self, room: "Room") -> None:
        await room.broadcast({"type": "_breakouts", "state": room.breakouts})
        await room.broadcast({"type": "breakouts", "breakouts": breakout_public(room, False), "manager": False})
        await room.broadcast({"type": "breakouts", "breakouts": breakout_public(room, True), "manager": True})

    async def configure(self, room: "Room", data: Dict[str, Any], by: str = "") -> None:
        people = [p for p in room.participants.values() if not p.can("breakout.manage")]
        rooms = data.get("rooms")
        if not rooms:
            n = max(1, min(50, int(data.get("count") or 2)))
            rooms = [{"name": f"Room {i + 1}", "members": []} for i in range(n)]
            if data.get("assign", "auto") == "auto":
                import random
                random.shuffle(people)
                for i, p in enumerate(people):
                    rooms[i % n]["members"].append(p.user_id)
        out = []
        for i, r in enumerate(rooms[:50]):
            members = []
            for m in r.get("members") or []:  # accept peer ids or user ids
                p = room.participants.get(m)
                members.append(p.user_id if p else str(m))
            out.append({"id": f"{room.id}~{i + 1}", "name": str(r.get("name") or f"Room {i + 1}")[:60],
                        "members": members})
        minutes = data.get("duration_minutes")
        room.breakouts = {"status": "draft", "rooms": out, "allow_choose": bool(data.get("allow_choose")),
                          "duration_minutes": int(minutes) if minutes else None, "ends_at": None, "by": by}
        await self._publish(room)

    def _token(self, room: "Room", p: "Participant", target: str) -> str:
        return self.meet.create_token(
            target, p.user_id, p.role, name=p.name, ttl=6 * 3600, tenant=room.config.tenant_id,
            grant=list(p.attributes.get("_grant") or []) + ["room.bypass_lobby"],
            revoke=list(p.attributes.get("_revoke") or []), meta={"breakout_of": room.id})

    def _find(self, room: "Room", user_id: str) -> Optional["Participant"]:
        return next((p for p in room.participants.values() if p.user_id == user_id), None)

    async def open(self, room: "Room") -> None:
        b = room.breakouts
        if not b or not b.get("rooms"):
            raise ValueError("configure breakout rooms first")
        cfg = room.config
        for r in b["rooms"]:
            if await self.meet.storage.get_room(r["id"]) is None:
                await self.meet.create_room(room_id=r["id"], name=r["name"], tenant_id=cfg.tenant_id,
                                            waiting_room=False, role_overrides=cfg.role_overrides,
                                            branding=cfg.branding, metadata={"breakout_of": room.id})
        b["status"] = "open"
        if b.get("duration_minutes"):
            b["ends_at"] = time.time() + 60 * b["duration_minutes"]
            self._timer(room, b["duration_minutes"] * 60)
        for r in b["rooms"]:
            for uid in r["members"]:
                p = self._find(room, uid)
                if p is not None:
                    await room.send_to(p.peer_id, {"type": "breakout-assign", "room": r["id"], "name": r["name"],
                                                   "token": self._token(room, p, r["id"])})
        await self._publish(room)

    def _timer(self, room: "Room", seconds: float) -> None:
        old = self._timers.pop(room.id, None)
        if old:
            old.cancel()

        async def run() -> None:
            await asyncio.sleep(max(0, seconds - 60))
            await self.close(room, seconds=min(60, int(seconds)))
        self._timers[room.id] = asyncio.ensure_future(run())

    async def send_pass(self, room: "Room", p: "Participant", sub_id: str) -> None:
        target = room.id if sub_id in ("", room.id) else sub_id
        name = next((r["name"] for r in (room.breakouts or {}).get("rooms", []) if r["id"] == target), "Main room")
        await room.send_to(p.peer_id, {"type": "breakout-assign", "room": target, "name": name,
                                       "token": self._token(room, p, target)})

    async def move(self, room: "Room", user_id: str, sub_id: str) -> None:
        b = room.breakouts or {}
        if sub_id not in [r["id"] for r in b.get("rooms", [])] + [room.id]:
            raise ValueError("unknown breakout room")
        for r in b.get("rooms", []):
            if user_id in r["members"]:
                r["members"].remove(user_id)
            if r["id"] == sub_id:
                r["members"].append(user_id)
        p = self._find(room, user_id)
        if p is not None and b.get("status") == "open":
            await self.send_pass(room, p, sub_id)
        else:  # they may already be in another breakout room
            for r in b.get("rooms", []):
                sub = self.meet.rooms.get(r["id"])
                q = self._find(sub, user_id) if sub else None
                if q is not None:
                    await sub.send_to(q.peer_id, {"type": "breakout-assign", "room": sub_id,
                                                  "token": self._token(room, q, sub_id), "name": ""})
        await self._publish(room)

    async def announce(self, room: "Room", text: str, by: str) -> None:
        msg = {"type": "announcement", "text": text, "by": by}
        await room.broadcast(msg)
        for r in (room.breakouts or {}).get("rooms", []):
            sub = self.meet.rooms.get(r["id"])
            if sub is not None:
                await sub.broadcast(msg)

    async def close(self, room: "Room", seconds: int = 30) -> None:
        b = room.breakouts
        if not b:
            return
        b["status"] = "closing"
        b["ends_at"] = time.time() + seconds
        await self._publish(room)
        for r in b.get("rooms", []):
            sub = self.meet.rooms.get(r["id"])
            if sub is not None:
                await sub.broadcast({"type": "breakout-closing", "seconds": seconds})
        if seconds:
            await asyncio.sleep(seconds)
        for r in b.get("rooms", []):
            sub = self.meet.rooms.get(r["id"])
            if sub is None:
                continue
            for p in list(sub.participants.values()):
                await sub.send_to(p.peer_id, {"type": "breakout-return", "room": room.id,
                                              "token": self._token(room, p, room.id)})
        room.breakouts = {}
        self._timers.pop(room.id, None)
        await self._publish(room)
