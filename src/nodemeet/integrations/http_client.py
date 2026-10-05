"""Tiny async HTTP client on the standard library (urllib in a thread).

Every integration (calendars, payments, SMS, SSO) takes ``http=HTTPClient(...)``;
tests pass ``HTTPClient(transport=fake)`` to check requests without the network.
"""
from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional


@dataclass
class HTTPResult:
    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Awaitable[HTTPResult]]


async def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> HTTPResult:
    def call() -> HTTPResult:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                return HTTPResult(resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as exc:
            return HTTPResult(exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()})
    return await asyncio.to_thread(call)


class HTTPError(RuntimeError):
    def __init__(self, result: HTTPResult, what: str = "") -> None:
        super().__init__(f"{what or 'request'} failed: HTTP {result.status} {result.text[:300]}")
        self.result = result


class HTTPClient:
    def __init__(self, transport: Optional[Transport] = None) -> None:
        self.transport = transport or urllib_transport

    async def request(self, method: str, url: str, *, params: Optional[Dict[str, Any]] = None,
                      json_body: Any = None, form: Optional[Dict[str, Any]] = None,
                      data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                      bearer: Optional[str] = None, basic: Optional[tuple] = None,
                      expect_ok: bool = False, what: str = "") -> HTTPResult:
        hdrs = dict(headers or {})
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True)
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif form is not None:
            body = urllib.parse.urlencode(form, doseq=True).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        if basic:
            hdrs["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        hdrs.setdefault("Accept", "application/json")
        hdrs.setdefault("User-Agent", "nodemeet")
        result = await self.transport(method, url, hdrs, body)
        if expect_ok and not result.ok:
            raise HTTPError(result, what)
        return result
