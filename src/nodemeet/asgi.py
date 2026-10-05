"""ASGI 3 adapter: run nodemeet under uvicorn/hypercorn/daphne, or mount it
inside FastAPI, Starlette, Django, Quart, Litestar...

FastAPI / Starlette::

    app = FastAPI()
    app.mount("/meet", meet.asgi())      # set NodeMeet(base_url="https://you.com/meet")

Mounted sub-apps don't receive lifespan events in Starlette, so nodemeet
starts itself on the first request. For deterministic startup/shutdown call
``await meet.startup()`` / ``await meet.shutdown()`` from your own lifespan.

Django (asgi.py)::

    from nodemeet.asgi import route
    application = route({"/meet": meet.asgi()}, default=get_asgi_application())
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import parse_qsl

from ._http import Request

log = logging.getLogger("nodemeet.asgi")
Scope = Dict[str, Any]
Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]
MAX_BODY = 4 * 1024 * 1024


def _split_path(scope: Scope) -> tuple:
    root = scope.get("root_path", "") or ""
    path = scope.get("path", "/") or "/"
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):]
    return (path or "/"), root.rstrip("/")


def _headers(scope: Scope) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _query(scope: Scope) -> Dict[str, str]:
    return dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))


class ASGIWebSocket:
    def __init__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._receive, self._send = receive, send
        self.query = _query(scope)
        self.headers = _headers(scope)
        client = scope.get("client")
        self.remote: Optional[str] = client[0] if client else None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def accept(self) -> bool:
        msg = await self._receive()
        if msg["type"] != "websocket.connect":
            self._closed = True
            return False
        await self._send({"type": "websocket.accept"})
        return True

    async def receive_text(self) -> Optional[str]:
        while not self._closed:
            msg = await self._receive()
            if msg["type"] == "websocket.receive":
                if msg.get("text") is not None:
                    return msg["text"]
                if msg.get("bytes") is not None:
                    return msg["bytes"].decode("utf-8", "replace")
            elif msg["type"] == "websocket.disconnect":
                self._closed = True
        return None

    async def send_text(self, data: str) -> None:
        if self._closed:
            return
        try:
            await self._send({"type": "websocket.send", "text": data})
        except Exception:  # noqa: BLE001 - client went away
            self._closed = True

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send({"type": "websocket.close", "code": code})
        except Exception:  # noqa: BLE001
            pass


class ASGIApp:
    def __init__(self, meet: Any) -> None:
        self.meet = meet

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)
        elif kind == "lifespan":
            await self._lifespan(receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                try:
                    await self.meet.startup()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:  # noqa: BLE001
                    log.exception("nodemeet startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
            elif msg["type"] == "lifespan.shutdown":
                await self.meet.shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body = msg.get("body", b"")
            size += len(body)
            if size > MAX_BODY:
                await self._send(send, 413, [("content-type", "application/json")],
                                 b'{"error":"payload_too_large"}')
                return
            chunks.append(body)
            if not msg.get("more_body"):
                break
        path, root = _split_path(scope)
        headers = _headers(scope)
        client = scope.get("client")
        req = Request(method=scope["method"].upper(), path=path, query=_query(scope),
                      headers=headers, body=b"".join(chunks), remote=client[0] if client else None,
                      prefix=root, scheme=scope.get("scheme", "http"), host=headers.get("host", ""))
        resp = await self.meet.handle(req)
        body = b"" if req.method == "HEAD" else resp.body
        await self._send(send, resp.status, resp.all_headers(), body)

    @staticmethod
    async def _send(send: Send, status: int, headers: list, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]})
        await send({"type": "http.response.body", "body": body})

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        path, _ = _split_path(scope)
        ws = ASGIWebSocket(scope, receive, send)
        if path.rstrip("/") != "/ws":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 4404})
            return
        if await ws.accept():
            await self.meet.handle_websocket(ws)
            await ws.close()


def route(routes: Dict[str, Any], default: Any = None) -> Any:
    """Tiny prefix dispatcher: ``route({"/meet": meet.asgi()}, default=django_app)``."""
    table = sorted(((p.rstrip("/"), a) for p, a in routes.items()), key=lambda x: -len(x[0]))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "/")
            for prefix, sub in table:
                if path == prefix or path.startswith(prefix + "/"):
                    root = (scope.get("root_path", "") or "") + prefix
                    await sub(dict(scope, root_path=root), receive, send)
                    return
        if default is None:
            if scope["type"] == "lifespan":
                return
            await ASGIApp._send(send, 404, [("content-type", "text/plain")], b"not found")
            return
        await default(scope, receive, send)

    return app
