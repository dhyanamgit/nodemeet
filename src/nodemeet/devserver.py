"""A small HTTP/1.1 + WebSocket server on the standard library that runs any ASGI app.

It means ``nodemeet serve`` and ``meet.run()`` work with **zero installed
packages** - great for development, demos, tests and small internal deployments.
For big production traffic put nodemeet behind uvicorn/hypercorn or aiohttp.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import struct
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

log = logging.getLogger("nodemeet.devserver")
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_BODY = 64 * 1024 * 1024
MAX_FRAME = 16 * 1024 * 1024


class _Conn:
    def __init__(self, app: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 server_addr: Tuple[str, int]) -> None:
        self.app, self.reader, self.writer, self.server_addr = app, reader, writer, server_addr
        peer = writer.get_extra_info("peername")
        self.client = (peer[0], peer[1]) if peer else None

    async def run(self) -> None:
        try:
            while True:
                head = await self._read_head()
                if head is None:
                    return
                method, target, headers = head
                if headers.get("upgrade", "").lower() == "websocket":
                    await self._websocket(target, headers)
                    return
                keep = await self._http(method, target, headers)
                if not keep:
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("connection error")
        finally:
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _read_head(self) -> Optional[Tuple[str, str, Dict[str, str]]]:
        try:
            raw = await self.reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None
        except asyncio.LimitOverrunError:
            self.writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\n\r\n")
            return None
        lines = raw.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            return None
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return method, target, headers

    def _scope(self, kind: str, target: str, headers: Dict[str, str], method: str = "GET") -> Dict[str, Any]:
        path, _, query = target.partition("?")
        return {"type": kind, "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                "scheme": "ws" if kind == "websocket" else "http", "path": unquote(path), "raw_path": path.encode(),
                "query_string": query.encode(), "root_path": "",
                "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
                "client": self.client, "server": self.server_addr}

    async def _body(self, headers: Dict[str, str]) -> bytes:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            out = b""
            while True:
                size = int((await self.reader.readuntil(b"\r\n")).split(b";")[0], 16)
                if size == 0:
                    await self.reader.readuntil(b"\r\n")
                    return out
                out += await self.reader.readexactly(size)
                await self.reader.readexactly(2)
                if len(out) > MAX_BODY:
                    raise ValueError("body too large")
        n = int(headers.get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return await self.reader.readexactly(n) if n else b""

    async def _http(self, method: str, target: str, headers: Dict[str, str]) -> bool:
        body = await self._body(headers)
        sent = {"done": False}
        status_line: List[bytes] = []

        async def receive() -> Dict[str, Any]:
            if sent.get("body_given"):
                await asyncio.sleep(3600)
                return {"type": "http.disconnect"}
            sent["body_given"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        chunks: List[bytes] = []

        async def send(msg: Dict[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                hdrs = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in msg.get("headers", [])]
                status_line.append(f"HTTP/1.1 {msg['status']} OK\r\n".encode())
                sent["headers"] = hdrs
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body"):
                    sent["done"] = True

        await self.app(self._scope("http", target, headers, method), receive, send)
        data = b"".join(chunks)
        hdrs = [h for h in sent.get("headers", []) if h[0].lower() not in ("content-length", "connection")]
        keep = headers.get("connection", "").lower() != "close"
        out = (status_line[0] if status_line else b"HTTP/1.1 500 OK\r\n")
        out += "".join(f"{k}: {v}\r\n" for k, v in hdrs).encode("latin-1")
        out += f"Content-Length: {len(data)}\r\nConnection: {'keep-alive' if keep else 'close'}\r\n\r\n".encode()
        self.writer.write(out + (b"" if method == "HEAD" else data))
        await self.writer.drain()
        return keep

    # -- WebSocket (RFC 6455) -----------------------------------------------------------------
    async def _websocket(self, target: str, headers: Dict[str, str]) -> None:
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        inbox: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        state = {"accepted": False, "closed": False}
        inbox.put_nowait({"type": "websocket.connect"})

        async def receive() -> Dict[str, Any]:
            return await inbox.get()

        async def send(msg: Dict[str, Any]) -> None:
            kind = msg["type"]
            if kind == "websocket.accept":
                self.writer.write((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                   f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                await self.writer.drain()
                state["accepted"] = True
                asyncio.ensure_future(reader_loop())
            elif kind == "websocket.send":
                if state["closed"]:
                    return
                if msg.get("text") is not None:
                    await self._frame(0x1, msg["text"].encode())
                else:
                    await self._frame(0x2, msg.get("bytes") or b"")
            elif kind == "websocket.close":
                if not state["accepted"]:
                    self.writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                elif not state["closed"]:
                    state["closed"] = True
                    await self._frame(0x8, struct.pack("!H", int(msg.get("code", 1000))))
                await self.writer.drain()

        async def reader_loop() -> None:
            parts: List[bytes] = []
            try:
                while True:
                    b1, b2 = await self.reader.readexactly(2)
                    fin, op = b1 & 0x80, b1 & 0x0F
                    n = b2 & 0x7F
                    if n == 126:
                        n = struct.unpack("!H", await self.reader.readexactly(2))[0]
                    elif n == 127:
                        n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
                    if n > MAX_FRAME:
                        break
                    mask = await self.reader.readexactly(4) if b2 & 0x80 else b""
                    payload = await self.reader.readexactly(n) if n else b""
                    if mask:
                        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                    if op == 0x8:
                        break
                    if op == 0x9:
                        await self._frame(0xA, payload)
                        continue
                    if op == 0xA:
                        continue
                    parts.append(payload)
                    if fin:
                        data = b"".join(parts)
                        parts = []
                        inbox.put_nowait({"type": "websocket.receive", "text": data.decode("utf-8", "replace")}
                                         if op in (0x1, 0x0) else {"type": "websocket.receive", "bytes": data})
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            if not state["closed"]:
                state["closed"] = True
                try:
                    await self._frame(0x8, struct.pack("!H", 1000))
                except Exception:  # noqa: BLE001
                    pass
            inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

        await self.app(self._scope("websocket", target, headers), receive, send)

    async def _frame(self, op: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack("!H", n)
        else:
            head += bytes([127]) + struct.pack("!Q", n)
        self.writer.write(head + payload)
        await self.writer.drain()


async def serve(app: Any, host: str = "127.0.0.1", port: int = 8080, *, lifespan: bool = True,
                ready: Optional[asyncio.Event] = None) -> None:
    """Serve ``app`` until cancelled."""
    if lifespan:
        await _lifespan(app, "startup")
    server = await asyncio.start_server(lambda r, w: _Conn(app, r, w, (host, port)).run(), host, port,
                                        limit=1 << 16)
    log.info("nodemeet dev server on http://%s:%s", host, port)
    if ready is not None:
        ready.set()
    try:
        async with server:
            await server.serve_forever()
    finally:
        if lifespan:
            await _lifespan(app, "shutdown")


async def _lifespan(app: Any, phase: str) -> None:
    q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
    q.put_nowait({"type": f"lifespan.{phase}"})
    done = asyncio.Event()

    async def receive() -> Dict[str, Any]:
        if done.is_set():
            await asyncio.sleep(3600)
        return await q.get()

    async def send(msg: Dict[str, Any]) -> None:
        if msg["type"].startswith(f"lifespan.{phase}"):
            done.set()

    task = asyncio.ensure_future(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
    await asyncio.wait_for(done.wait(), 30)
    if phase == "shutdown":
        task.cancel()
    else:
        app._nm_lifespan_task = task  # keep it alive


def run(app: Any, host: str = "127.0.0.1", port: int = 8080) -> None:
    try:
        asyncio.run(serve(app, host, port))
    except KeyboardInterrupt:
        pass
