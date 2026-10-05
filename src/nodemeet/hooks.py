"""Event hooks: plug your own logic (auth, moderation, analytics, billing...).

Handlers may be sync or async functions.

* ``before_join(ctx)`` -- return ``False`` or raise :class:`JoinRejected` to refuse.
  ``ctx`` is a :class:`JoinContext`; you may change ``ctx.name`` / ``ctx.role``.
* ``on_join(room, participant)`` / ``on_leave(room, participant)``
* ``on_chat(room, participant, message)`` -- return ``False`` to drop the message,
  a ``str`` to replace its text, or ``None`` to keep it.
* ``on_hand_raised(room, participant, raised)`` / ``on_reaction(room, participant, emoji)``
* ``on_lobby(room, participant)`` / ``on_admitted(room, participant, by)``
* ``on_moderation(room, actor, action, target_or_None, data)``
* ``on_role_changed(room, participant, old_role)``
* ``on_room_created(room)`` / ``on_room_closed(room)``
* ``on_booking_created(booking)`` / ``on_booking_cancelled(booking)`` /
  ``on_booking_rescheduled(booking, old_start)`` / ``on_reminder(booking, minutes)``
"""
from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Set

from .exceptions import JoinRejected
from .tokens import TokenClaims

log = logging.getLogger("nodemeet.hooks")

EVENTS = (
    "before_join", "on_join", "on_leave", "on_chat", "on_hand_raised",
    "on_room_created", "on_room_closed", "on_lobby", "on_admitted", "on_moderation",
    "on_role_changed", "on_reaction", "on_blocked_attempt",
    "on_booking_created", "on_booking_cancelled", "on_booking_rescheduled", "on_reminder",
)


@dataclass
class JoinContext:
    """Passed to ``before_join``. Change ``name``, ``role``, ``permissions`` or
    ``attributes`` to adjust how this person joins."""

    room_id: str
    claims: TokenClaims
    name: str
    role: str
    remote: Optional[str] = None  # client IP if known
    headers: Dict[str, str] = field(default_factory=dict)
    participant_count: int = 0
    permissions: Set[str] = field(default_factory=set)
    attributes: Dict[str, Any] = field(default_factory=dict)


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


class Hooks:
    def __init__(self) -> None:
        self._handlers: DefaultDict[str, List[Callable[..., Any]]] = defaultdict(list)

    def on(self, event: str, fn: Optional[Callable[..., Any]] = None) -> Any:
        """Register a handler. Usable as ``hooks.on("on_join", fn)`` or a decorator."""
        from .easy import event_name
        event = event_name(event, EVENTS)  # "join", "on_join", "booked"... all work

        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            self._handlers[event].append(f)
            return f

        return register(fn) if fn is not None else register

    def off(self, event: str, fn: Callable[..., Any]) -> None:
        if fn in self._handlers.get(event, []):
            self._handlers[event].remove(fn)

    def handlers(self, event: str) -> List[Callable[..., Any]]:
        return list(self._handlers.get(event, []))

    # Decorator shorthands: @hooks.on_join, @meet.before_join, ...
    def before_join(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.on("before_join", fn)

    def on_join(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.on("on_join", fn)

    def on_leave(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.on("on_leave", fn)

    def on_chat(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.on("on_chat", fn)

    async def emit(self, event: str, *args: Any) -> None:
        """Fire-and-log: handler errors never break the meeting."""
        for fn in self.handlers(event):
            try:
                await _call(fn, *args)
            except Exception:  # noqa: BLE001
                log.exception("hook %s handler %r failed", event, fn)

    async def run_before_join(self, ctx: JoinContext) -> None:
        """Raises JoinRejected if any handler refuses."""
        for fn in self.handlers("before_join"):
            result = await _call(fn, ctx)
            if result is False:
                raise JoinRejected("join rejected by server policy")

    async def run_on_chat(self, room: Any, participant: Any, text: str) -> Optional[str]:
        """Returns the (possibly rewritten) text, or None if a handler dropped it."""
        for fn in self.handlers("on_chat"):
            try:
                result = await _call(fn, room, participant, text)
            except Exception:  # noqa: BLE001
                log.exception("on_chat handler %r failed", fn)
                continue
            if result is False:
                return None
            if isinstance(result, str):
                text = result
        return text
