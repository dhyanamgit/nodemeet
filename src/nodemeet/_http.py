"""Framework-neutral HTTP primitives shared by the aiohttp and ASGI adapters."""
from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import unquote


@dataclass
class Request:
    method: str
    path: str  # path *inside* nodemeet (mount prefix removed)
    query: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)  # lower-case keys
    body: bytes = b""
    remote: Optional[str] = None
    prefix: str = ""  # mount prefix, e.g. "/meet"
    scheme: str = "http"
    host: str = ""
    match_info: Dict[str, str] = field(default_factory=dict)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPError(400, "bad_request", "request body must be JSON") from None
        if not isinstance(data, dict):
            raise HTTPError(400, "bad_request", "request body must be a JSON object")
        return data

    def base_url(self, trust_proxy: bool = False) -> str:
        scheme, host = self.scheme, self.host or self.header("host")
        if trust_proxy:
            scheme = self.header("x-forwarded-proto", scheme).split(",")[0].strip() or scheme
            host = self.header("x-forwarded-host", host).split(",")[0].strip() or host
        return f"{scheme}://{host}{self.prefix}"


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: Dict[str, str] = field(default_factory=dict)

    def all_headers(self) -> List[Tuple[str, str]]:
        ct = self.content_type
        if ct.startswith("text/") or ct in ("application/json", "application/javascript"):
            ct += "; charset=utf-8"
        out = [("content-type", ct), ("content-length", str(len(self.body)))]
        return out + [(k.lower(), v) for k, v in self.headers.items()]


class HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str = "",
                 headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code
        self.headers = headers or {}

    def response(self) -> Response:
        return json_response({"error": self.code, "message": self.message}, self.status,
                             headers=self.headers)


def json_response(data: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(status, json.dumps(data, default=str).encode(), "application/json",
                    dict(headers or {}))


def text_response(text: str, content_type: str = "text/plain", status: int = 200,
                  headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(status, text.encode("utf-8"), content_type, dict(headers or {}))


Handler = Callable[[Request], Awaitable[Response]]


class Router:
    def __init__(self) -> None:
        self.routes: List[Tuple[str, Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self.routes.append((method.upper(), re.compile(regex), handler))

    def match(self, method: str, path: str) -> Tuple[Handler, Dict[str, str]]:
        allowed = False
        for m, rx, handler in self.routes:
            found = rx.match(path)
            if found:
                if m == method or (m == "GET" and method == "HEAD"):
                    return handler, {k: unquote(v) for k, v in found.groupdict().items()}
                allowed = True
        if allowed:
            raise HTTPError(405, "method_not_allowed")
        raise HTTPError(404, "not_found")


def static_response(root: Path, rel: str) -> Response:
    target = (root / rel).resolve()
    if root.resolve() not in target.parents or not target.is_file():
        raise HTTPError(404, "not_found")
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".js":
        ctype = "application/javascript"
    return Response(200, target.read_bytes(), ctype, {"Cache-Control": "public, max-age=300"})
