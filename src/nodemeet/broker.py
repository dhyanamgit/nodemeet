"""Cross-server coordination (pub/sub, shared state, locks, counters).

* :class:`MemoryBroker` -- single process (default). Several instances can
  share one ``MemoryHub`` to simulate a cluster in tests.
* :class:`RedisBroker` -- many servers behind a load balancer
  (``pip install "nodemeet[redis]"``).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple

log = logging.getLogger("nodemeet.broker")
Handler = Callable[[Dict[str, Any]], Awaitable[None]]


class LockTimeout(Exception):
    """Could not acquire a distributed lock in time."""


class Broker:
    """Interface. Subclass to use NATS, Postgres LISTEN/NOTIFY, etc."""

    node_id: str
    clustered = False

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def publish(self, channel: str, message: Dict[str, Any]) -> None: raise NotImplementedError
    async def subscribe(self, channel: str, handler: Handler) -> None: raise NotImplementedError
    async def unsubscribe(self, channel: str) -> None: raise NotImplementedError
    async def hset(self, key: str, field: str, value: str) -> None: raise NotImplementedError
    async def hdel(self, key: str, field: str) -> None: raise NotImplementedError
    async def hgetall(self, key: str) -> Dict[str, str]: raise NotImplementedError
    async def set(self, key: str, value: str, *, ttl: Optional[float] = None,
                  nx: bool = False) -> bool: raise NotImplementedError
    async def get(self, key: str) -> Optional[str]: raise NotImplementedError
    async def delete(self, key: str) -> None: raise NotImplementedError
    async def incr(self, key: str, *, ttl: Optional[float] = None) -> int: raise NotImplementedError

    @contextlib.asynccontextmanager
    async def lock(self, name: str, *, ttl: float = 15.0, timeout: float = 10.0) -> AsyncIterator[None]:
        token = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        key = f"lock:{name}"
        while not await self.set(key, token, ttl=ttl, nx=True):
            if time.monotonic() > deadline:
                raise LockTimeout(name)
            await asyncio.sleep(0.02)
        try:
            yield
        finally:
            await self._release(key, token)

    async def _release(self, key: str, token: str) -> None:
        if await self.get(key) == token:
            await self.delete(key)


class MemoryHub:
    """Shared state for MemoryBroker instances in one process."""

    def __init__(self) -> None:
        self.subs: Dict[str, Dict[str, Handler]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.values: Dict[str, Tuple[str, Optional[float]]] = {}


class MemoryBroker(Broker):
    def __init__(self, hub: Optional[MemoryHub] = None, node_id: Optional[str] = None) -> None:
        self.hub = hub or MemoryHub()
        self.node_id = node_id or "node_" + uuid.uuid4().hex[:8]
        self.clustered = hub is not None  # sharing a hub == simulated cluster

    async def publish(self, channel: str, message: Dict[str, Any]) -> None:
        data = json.loads(json.dumps(message, default=str))  # same semantics as the wire
        for node, handler in list(self.hub.subs.get(channel, {}).items()):
            try:
                await handler(data)
            except Exception:  # noqa: BLE001
                log.exception("broker handler failed on %s", node)

    async def subscribe(self, channel: str, handler: Handler) -> None:
        self.hub.subs.setdefault(channel, {})[self.node_id] = handler

    async def unsubscribe(self, channel: str) -> None:
        self.hub.subs.get(channel, {}).pop(self.node_id, None)

    async def hset(self, key: str, field: str, value: str) -> None:
        self.hub.hashes.setdefault(key, {})[field] = value

    async def hdel(self, key: str, field: str) -> None:
        self.hub.hashes.get(key, {}).pop(field, None)

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hub.hashes.get(key, {}))

    def _alive(self, key: str) -> Optional[str]:
        item = self.hub.values.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires < time.monotonic():
            self.hub.values.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, *, ttl: Optional[float] = None,
                  nx: bool = False) -> bool:
        if nx and self._alive(key) is not None:
            return False
        self.hub.values[key] = (value, time.monotonic() + ttl if ttl else None)
        return True

    async def get(self, key: str) -> Optional[str]:
        return self._alive(key)

    async def delete(self, key: str) -> None:
        self.hub.values.pop(key, None)

    async def incr(self, key: str, *, ttl: Optional[float] = None) -> int:
        current = int(self._alive(key) or 0) + 1
        item = self.hub.values.get(key)
        expires = item[1] if item and item[1] else (time.monotonic() + ttl if ttl else None)
        self.hub.values[key] = (str(current), expires)
        return current


_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisBroker(Broker):
    """Redis-backed broker (works with Redis, Valkey, KeyDB, Dragonfly - all OSS)."""

    clustered = True

    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "nodemeet:",
                 client: Any = None, node_id: Optional[str] = None) -> None:
        self.url = url
        self.prefix = prefix
        self.node_id = node_id or "node_" + uuid.uuid4().hex[:8]
        self.r = client
        self._pubsub: Any = None
        self._handlers: Dict[str, Handler] = {}
        self._task: Optional["asyncio.Task[None]"] = None

    def k(self, key: str) -> str:
        return self.prefix + key

    async def start(self) -> None:
        if self.r is None:
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError('install the redis extra: pip install "nodemeet[redis]"') from exc
            self.r = aioredis.from_url(self.url, decode_responses=True)
        self._pubsub = self.r.pubsub()
        self._task = asyncio.ensure_future(self._reader())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose() if hasattr(self._pubsub, "aclose") else await self._pubsub.close()
        if self.r is not None:
            with contextlib.suppress(Exception):
                await self.r.aclose() if hasattr(self.r, "aclose") else await self.r.close()

    async def _reader(self) -> None:
        while True:
            if not self._handlers:
                await asyncio.sleep(0.05)
                continue
            try:
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("redis pubsub read failed; retrying")
                await asyncio.sleep(1.0)
                continue
            if not msg or msg.get("type") != "message":
                continue
            channel = str(msg["channel"])[len(self.prefix):]
            handler = self._handlers.get(channel)
            if handler is not None:
                try:
                    await handler(json.loads(msg["data"]))
                except Exception:  # noqa: BLE001
                    log.exception("broker handler for %s failed", channel)

    async def publish(self, channel: str, message: Dict[str, Any]) -> None:
        await self.r.publish(self.k(channel), json.dumps(message, default=str))

    async def subscribe(self, channel: str, handler: Handler) -> None:
        self._handlers[channel] = handler
        await self._pubsub.subscribe(self.k(channel))

    async def unsubscribe(self, channel: str) -> None:
        if self._handlers.pop(channel, None) is not None:
            await self._pubsub.unsubscribe(self.k(channel))

    async def hset(self, key: str, field: str, value: str) -> None:
        await self.r.hset(self.k(key), field, value)

    async def hdel(self, key: str, field: str) -> None:
        await self.r.hdel(self.k(key), field)

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(await self.r.hgetall(self.k(key)))

    async def set(self, key: str, value: str, *, ttl: Optional[float] = None,
                  nx: bool = False) -> bool:
        px = int(ttl * 1000) if ttl else None
        return bool(await self.r.set(self.k(key), value, px=px, nx=nx))

    async def get(self, key: str) -> Optional[str]:
        return await self.r.get(self.k(key))

    async def delete(self, key: str) -> None:
        await self.r.delete(self.k(key))

    async def incr(self, key: str, *, ttl: Optional[float] = None) -> int:
        value = int(await self.r.incr(self.k(key)))
        if value == 1 and ttl:
            await self.r.pexpire(self.k(key), int(ttl * 1000))
        return value

    async def _release(self, key: str, token: str) -> None:
        await self.r.eval(_RELEASE_LUA, 1, self.k(key), token)
