"""Signed outgoing webhooks.

Each request carries::

    X-NodeMeet-Event: booking.created
    X-NodeMeet-Delivery: <uuid>
    X-NodeMeet-Signature: t=1700000000,v1=<hex hmac-sha256 of "t.body">

Verify on the receiving side with :func:`verify_signature`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Union

log = logging.getLogger("nodemeet.webhooks")

SIGNATURE_HEADER = "X-NodeMeet-Signature"
EVENT_HEADER = "X-NodeMeet-Event"
DELIVERY_HEADER = "X-NodeMeet-Delivery"

from ..events import EVENTS, wants as _wants  # noqa: E402  (full catalogue: nodemeet.events)

# (url, body, headers, timeout) -> HTTP status code
Transport = Callable[[str, bytes, Dict[str, str], float], Awaitable[int]]


def _key(secret: Union[str, bytes]) -> bytes:
    return secret.encode() if isinstance(secret, str) else secret


def sign_payload(secret: Union[str, bytes], body: bytes, timestamp: Optional[int] = None) -> str:
    ts = int(time.time() if timestamp is None else timestamp)
    mac = hmac.new(_key(secret), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(secret: Union[str, bytes], body: bytes, header: str, *,
                     tolerance: int = 300, now: Optional[float] = None) -> bool:
    """Constant-time check of a ``X-NodeMeet-Signature`` header."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError, AttributeError):
        return False
    current = time.time() if now is None else now
    if tolerance and abs(current - ts) > tolerance:
        return False
    expected = sign_payload(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(expected, given)


@dataclass
class WebhookEndpoint:
    url: str
    secret: str
    events: Optional[Set[str]] = None  # None = everything
    id: str = field(default_factory=lambda: "wh_" + uuid.uuid4().hex[:16])
    tenant_id: Optional[str] = None  # None = platform-level endpoint (gets every event)

    def wants(self, event: str) -> bool:
        return _wants(self.events, event)

    def to_dict(self, *, include_secret: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "url": self.url, "tenant_id": self.tenant_id,
                             "events": sorted(self.events) if self.events else None}
        if include_secret:
            d["secret"] = self.secret
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WebhookEndpoint":
        ev = d.get("events")
        return cls(url=d["url"], secret=d["secret"], events=set(ev) if ev else None,
                   id=d.get("id") or "wh_" + uuid.uuid4().hex[:16], tenant_id=d.get("tenant_id"))


async def aiohttp_transport(url: str, body: bytes, headers: Dict[str, str], timeout: float) -> int:
    import aiohttp  # local import keeps the module importable without aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, data=body, headers=headers) as resp:
            return resp.status


async def urllib_transport(url: str, body: bytes, headers: Dict[str, str], timeout: float) -> int:
    """Stdlib fallback (runs in a thread) so webhooks work without aiohttp."""
    import urllib.error
    import urllib.request

    def post() -> int:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    return await asyncio.to_thread(post)


def default_transport() -> Transport:
    try:
        import aiohttp  # noqa: F401
        return aiohttp_transport
    except ImportError:
        return urllib_transport


# async (event, tenant_id) -> extra endpoints, e.g. tenant endpoints from storage
Resolver = Callable[[str, Optional[str]], Awaitable[List[WebhookEndpoint]]]


class WebhookDispatcher:
    """Delivers events to endpoints with retries and exponential backoff.

    Deliveries run in the background by default so a slow receiver never
    blocks a meeting. Use ``await dispatcher.emit(..., wait=True)`` in tests.
    """

    def __init__(self, endpoints: Iterable[WebhookEndpoint] = (), *, retries: int = 3,
                 backoff: float = 0.5, timeout: float = 10.0,
                 transport: Optional[Transport] = None,
                 resolver: Optional[Resolver] = None) -> None:
        self.endpoints: List[WebhookEndpoint] = list(endpoints)
        self.retries, self.backoff, self.timeout = retries, backoff, timeout
        self.transport: Transport = transport or default_transport()
        self.resolver = resolver
        self._tasks: Set["asyncio.Task[Any]"] = set()
        # in-process subscribers to every event (Slack/Discord/Teams, push...): async (event, data, tenant_id)
        self.taps: List[Callable[..., Any]] = []
        # async (record) -> None: delivery log (NodeMeet stores it; GET /api/webhooks/deliveries)
        self.recorder: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    def add(self, url: str, secret: str, events: Optional[Iterable[str]] = None) -> WebhookEndpoint:
        ep = WebhookEndpoint(url, secret, set(events) if events else None)
        self.endpoints.append(ep)
        return ep

    def build(self, event: str, data: Dict[str, Any], tenant_id: Optional[str] = None) -> bytes:
        envelope = {"id": uuid.uuid4().hex, "event": event, "created": int(time.time()),
                    "tenant_id": tenant_id, "data": data}
        return json.dumps(envelope, separators=(",", ":"), default=str).encode()

    async def _deliver(self, ep: WebhookEndpoint, event: str, body: bytes) -> bool:
        started, _out = time.time(), {}  # type: ignore[var-annotated]
        ok = await self._attempts(ep, event, body, _out)
        if self.recorder is not None:
            env = json.loads(body)
            try:
                await self.recorder({"delivery_id": env["id"], "event": event, "url": ep.url,
                                     "webhook_id": getattr(ep, "id", None), "tenant_id": env.get("tenant_id"),
                                     "ok": ok, "status": _out.get("status"), "error": _out.get("error"),
                                     "attempts": _out.get("attempts"), "at": started,
                                     "ms": round((time.time() - started) * 1000), "body": body.decode()})
            except Exception:  # noqa: BLE001
                log.exception("webhook delivery log failed")
        return ok

    async def _attempts(self, ep: WebhookEndpoint, event: str, body: bytes, out: Dict[str, Any]) -> bool:
        delivery = json.loads(body)["id"]
        for attempt in range(self.retries + 1):
            out["attempts"] = attempt + 1
            headers = {"Content-Type": "application/json", EVENT_HEADER: event,
                       DELIVERY_HEADER: delivery, SIGNATURE_HEADER: sign_payload(ep.secret, body),
                       "User-Agent": "nodemeet-webhooks"}
            try:
                status = await self.transport(ep.url, body, headers, self.timeout)
                out["status"] = status
                if 200 <= status < 300:
                    return True
                log.warning("webhook %s -> %s returned %s", event, ep.url, status)
            except Exception as exc:  # noqa: BLE001
                out["error"] = str(exc)[:300]
                log.warning("webhook %s -> %s failed: %s", event, ep.url, exc)
            if attempt < self.retries:
                await asyncio.sleep(self.backoff * (2 ** attempt))
        return False

    async def emit(self, event: str, data: Dict[str, Any], *, wait: bool = False,
                   tenant_id: Optional[str] = None) -> List[bool]:
        for tap in list(self.taps):
            task = asyncio.ensure_future(self._tap(tap, event, data, tenant_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        targets = [ep for ep in self.endpoints if ep.wants(event)]
        if self.resolver is not None:
            try:
                extra = await self.resolver(event, tenant_id)
            except Exception:  # noqa: BLE001
                log.exception("webhook resolver failed")
                extra = []
            targets += [ep for ep in extra if ep.wants(event) and ep.tenant_id == tenant_id]
        if not targets:
            return []
        body = self.build(event, data, tenant_id)
        coro = asyncio.gather(*(self._deliver(ep, event, body) for ep in targets))
        if wait:
            return list(await coro)
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return []

    async def _tap(self, tap: Callable[..., Any], event: str, data: Dict[str, Any],
                   tenant_id: Optional[str]) -> None:
        try:
            await tap(event, data, tenant_id)
        except Exception:  # noqa: BLE001 - a broken integration must never break meetings
            log.exception("event subscriber %r failed for %s", tap, event)

    async def redeliver(self, ep: WebhookEndpoint, body: bytes) -> bool:
        """Send a stored delivery again (same id, fresh signature)."""
        return await self._deliver(ep, json.loads(body)["event"], body)

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for background deliveries and subscribers (tests, graceful shutdown)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                await asyncio.sleep(0)  # let follow-up events schedule their own tasks
                if not [t for t in self._tasks if not t.done()]:
                    return
                continue
            await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
