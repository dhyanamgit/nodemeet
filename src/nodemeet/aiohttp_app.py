"""aiohttp adapter (``pip install "nodemeet[aiohttp]"``)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional

from aiohttp import WSMsgType, web

from ._http import Request

if TYPE_CHECKING:
    from .server import NodeMeet


class AiohttpWebSocket:
    def __init__(self, ws: web.WebSocketResponse, request: web.Request) -> None:
        self.ws = ws
        self.query: Dict[str, str] = dict(request.query)
        self.headers: Dict[str, str] = {k.lower(): v for k, v in request.headers.items()}
        self.remote: Optional[str] = request.remote

    @property
    def closed(self) -> bool:
        return self.ws.closed

    async def receive_text(self) -> Optional[str]:
        while True:
            msg = await self.ws.receive()
            if msg.type == WSMsgType.TEXT:
                return msg.data
            if msg.type == WSMsgType.BINARY:
                return msg.data.decode("utf-8", "replace")
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                return None

    async def send_text(self, data: str) -> None:
        if not self.ws.closed:
            await self.ws.send_str(data)

    async def close(self, code: int = 1000) -> None:
        await self.ws.close(code=code)


def build_app(meet: "NodeMeet") -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    import warnings
    with warnings.catch_warnings():  # keep app["nodemeet"] working without aiohttp's AppKey warning
        warnings.simplefilter("ignore")
        app["nodemeet"] = meet


    async def ws_handler(request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=25.0, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        await meet.handle_websocket(AiohttpWebSocket(ws, request))
        return ws

    async def http_handler(request: web.Request) -> web.StreamResponse:
        path = request.path
        if meet.prefix and (path == meet.prefix or path.startswith(meet.prefix + "/")):
            path = path[len(meet.prefix):] or "/"
        req = Request(method=request.method, path=path, query=dict(request.query),
                      headers={k.lower(): v for k, v in request.headers.items()},
                      body=await request.read(), remote=request.remote, prefix=meet.prefix,
                      scheme=request.scheme, host=request.host)
        resp = await meet.handle(req)
        headers = {k: v for k, v in resp.all_headers() if k != "content-length"}
        return web.Response(status=resp.status, body=resp.body, headers=headers)

    async def on_startup(app: web.Application) -> None:
        await meet.startup()

    async def on_shutdown(app: web.Application) -> None:
        await meet.shutdown()

    app.router.add_get("/ws", ws_handler)
    app.router.add_route("*", "/{tail:.*}", http_handler)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app
