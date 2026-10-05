"""Webhook tooling: event catalogue, delivery log + redelivery, test events and a
long-poll event feed (for places that can't receive webhooks).

    GET  /api/webhooks/events                    every event with a description
    GET  /api/webhooks/deliveries?event=&failed=1&limit=50
    POST /api/webhooks/deliveries/{id}/redeliver
    POST /api/webhooks/test   {"event": "booking.created", "webhook_id": "..."}
    GET  /api/events?after=<seq>&wait=25&events=booking.*,payment.*
"""
from __future__ import annotations

import asyncio
import collections
import json
import time
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

from ._http import HTTPError, Request, Response, Router, json_response
from .events import catalog, wants

if TYPE_CHECKING:
    from .server import NodeMeet

KEEP_DELIVERIES = 1000


class WebhookTools:
    def __init__(self, meet: "NodeMeet", buffer: int = 1000) -> None:
        self.meet = meet
        self.feed: Deque[Dict[str, Any]] = collections.deque(maxlen=buffer)
        self.seq = 0
        self._new = asyncio.Event()
        self._writes = 0
        meet.webhooks.taps.append(self.on_event)
        meet.webhooks.recorder = self.record

    async def on_event(self, event: str, data: Dict[str, Any], tenant_id: Optional[str]) -> None:
        self.seq += 1
        self.feed.append({"seq": self.seq, "event": event, "created": time.time(), "tenant_id": tenant_id, "data": data})
        self._new.set()
        self._new = asyncio.Event()

    async def record(self, rec: Dict[str, Any]) -> None:
        key = f"{rec['at']:017.6f}|{rec['delivery_id']}|{rec.get('webhook_id') or 'static'}"
        await self.meet.storage.put_record("webhook_delivery", key, rec)
        self._writes += 1
        if self._writes % 100 == 0:  # prune
            rows = await self.meet.storage.list_records("webhook_delivery")
            for k, _ in rows[:-KEEP_DELIVERIES]:
                await self.meet.storage.delete_record("webhook_delivery", k)

    def register(self, r: Router) -> None:
        r.add("GET", "/api/webhooks/events", self.http_events)
        r.add("GET", "/api/webhooks/deliveries", self.http_deliveries)
        r.add("POST", "/api/webhooks/deliveries/{delivery_id}/redeliver", self.http_redeliver)
        r.add("POST", "/api/webhooks/test", self.http_test)
        r.add("GET", "/api/events", self.http_feed)

    async def _endpoints(self, tenant: Optional[str], is_master: bool) -> List[Any]:
        stored = await self.meet.storage.list_webhooks(None if is_master else tenant)
        static = list(self.meet.webhooks.endpoints) if is_master else []
        return static + list(stored)

    async def http_events(self, req: Request) -> Response:
        return json_response({"events": catalog(), "patterns": ["*", "booking.*", "*.failed", "custom.*"],
                              "note": "noisy events are only sent when named or matched by a non-* pattern"})

    async def http_deliveries(self, req: Request) -> Response:
        p = await self.meet.api.require(req, "webhooks:manage")
        rows = await self.meet.storage.list_records("webhook_delivery")
        ev, failed = req.query.get("event"), req.query.get("failed") in ("1", "true", "yes")
        limit = min(int(req.query.get("limit", 50)), 500)
        out = []
        for key, rec in reversed(rows):
            if not p.is_master and rec.get("tenant_id") != p.tenant_id:
                continue
            if ev and rec["event"] != ev or failed and rec.get("ok"):
                continue
            out.append({"id": key, **{k: v for k, v in rec.items() if k != "body"}})
            if len(out) >= limit:
                break
        return json_response({"deliveries": out})

    async def http_redeliver(self, req: Request) -> Response:
        p = await self.meet.api.require(req, "webhooks:manage")
        rec = await self.meet.storage.get_record("webhook_delivery", req.match_info["delivery_id"])
        if rec is None or (not p.is_master and rec.get("tenant_id") != p.tenant_id):
            raise HTTPError(404, "not_found", "no such delivery")
        eps = [e for e in await self._endpoints(p.tenant_id, p.is_master)
               if (rec.get("webhook_id") and getattr(e, "id", None) == rec["webhook_id"]) or e.url == rec["url"]]
        if not eps:
            raise HTTPError(410, "endpoint_gone", "that webhook endpoint no longer exists")
        ok = await self.meet.webhooks.redeliver(eps[0], rec["body"].encode())
        return json_response({"ok": ok})

    async def http_test(self, req: Request) -> Response:
        p = await self.meet.api.require(req, "webhooks:manage")
        body = req.json() or {}
        event = str(body.get("event") or "webhook.test")
        sample = {"test": True, "event": event, "message": "This is a test event from nodemeet", "at": time.time()}
        targets = [e for e in await self._endpoints(p.tenant_id, p.is_master)
                   if not body.get("webhook_id") or getattr(e, "id", None) == body["webhook_id"]]
        results = []
        for ep in targets:
            raw = self.meet.webhooks.build(event, sample, None if p.is_master else p.tenant_id)
            results.append({"url": ep.url, "ok": await self.meet.webhooks._deliver(ep, event, raw)})
        return json_response({"event": event, "results": results})

    async def http_feed(self, req: Request) -> Response:
        p = await self.meet.api.require(req, "webhooks:manage")
        after = int(req.query.get("after", 0) or 0)
        wait = min(float(req.query.get("wait", 0) or 0), 55.0)
        pats = [x.strip() for x in req.query.get("events", "").split(",") if x.strip()] or None

        def pick() -> List[Dict[str, Any]]:
            return [e for e in self.feed if e["seq"] > after and wants(pats, e["event"])
                    and (p.is_master or e["tenant_id"] == p.tenant_id)]

        items = pick()
        deadline = time.monotonic() + wait
        while not items and time.monotonic() < deadline:
            try:
                await asyncio.wait_for(self._new.wait(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            items = pick()
        return json_response({"events": json.loads(json.dumps(items[:200], default=str)),
                              "next": items[-1]["seq"] if items else max(after, 0), "latest": self.seq})
