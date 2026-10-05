"""A tiny in-process ASGI test client (no httpx/starlette needed)."""
import asyncio
import json as _json
from urllib.parse import urlsplit


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, dict(headers), body

    def json(self):
        return _json.loads(self.body) if self.body else None

    @property
    def text(self):
        return self.body.decode()


class WSClosed(Exception):
    pass


class WS:
    def __init__(self, app, scope):
        self.app, self.scope = app, scope
        self.inbox, self.outbox = asyncio.Queue(), asyncio.Queue()
        self.task = None
        self.closed = False

    async def connect(self):
        await self.inbox.put({"type": "websocket.connect"})
        async def send(msg):
            if msg["type"] == "websocket.close":  # like a real server: then report disconnect
                await self.inbox.put({"type": "websocket.disconnect", "code": msg.get("code", 1000)})
            await self.outbox.put(msg)

        self.task = asyncio.ensure_future(self.app(self.scope, self.inbox.get, send))
        msg = await asyncio.wait_for(self.outbox.get(), 3)
        if msg["type"] != "websocket.accept":
            self.closed = True
        return self

    async def send_json(self, data):
        await self.inbox.put({"type": "websocket.receive", "text": _json.dumps(data)})

    async def receive_json(self, timeout=3.0):
        msg = await asyncio.wait_for(self.outbox.get(), timeout)
        if msg["type"] == "websocket.close":
            self.closed = True
            raise WSClosed(msg.get("code"))
        return _json.loads(msg["text"])

    async def recv_until(self, kind, timeout=3.0):
        while True:
            msg = await self.receive_json(timeout)
            if msg["type"] == kind:
                return msg

    async def close(self):
        await self.inbox.put({"type": "websocket.disconnect", "code": 1000})
        if self.task:
            await asyncio.wait_for(self.task, 3)
        self.closed = True


class ASGIClient:
    def __init__(self, app, root_path=""):
        self.app, self.root = app, root_path

    def _scope(self, kind, path, headers):
        parts = urlsplit(path)
        return {"type": kind, "path": self.root + parts.path, "root_path": self.root,
                "query_string": parts.query.encode(), "scheme": "http", "client": ("127.0.0.1", 5000),
                "headers": [(k.lower().encode(), v.encode()) for k, v in
                            {"host": "test", **(headers or {})}.items()]}

    async def request(self, method, path, json=None, headers=None, body=None):
        scope = self._scope("http", path, headers)
        scope["method"] = method
        if body is None:
            body = _json.dumps(json).encode() if json is not None else b""
        sent = []
        first = [True]

        async def receive():
            if first[0]:
                first[0] = False
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)

        async def send(msg):
            sent.append(msg)

        await self.app(scope, receive, send)
        start = sent[0]
        hdrs = [(k.decode(), v.decode()) for k, v in start["headers"]]
        return Resp(start["status"], hdrs, b"".join(m.get("body", b"") for m in sent[1:]))

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, json=None, **kw):
        return await self.request("POST", path, json=json if json is not None else {}, **kw)

    async def ws(self, path="/ws", headers=None):
        return await WS(self.app, self._scope("websocket", path, headers)).connect()
