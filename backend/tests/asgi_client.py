"""Minimal ASGI test client (httpx/TestClient are unavailable in the build sandbox).

Supports JSON bodies, raw bodies, custom headers/client address, and a cookie jar. Each request
runs on a fresh event loop; the app under test keeps no loop-bound state.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode


class Response:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        self.status = status
        self.raw_headers = headers
        self.body = body

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in self.raw_headers}

    def header_list(self, name: str) -> list[str]:
        name = name.lower()
        return [v.decode("latin-1") for k, v in self.raw_headers if k.decode("latin-1").lower() == name]

    def json(self) -> Any:
        return jsonlib.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class AsgiClient:
    def __init__(
        self,
        app: Any,
        *,
        client: tuple[str, int] = ("127.0.0.1", 50000),
        host: str = "localhost",
        scheme: str = "http",
    ) -> None:
        self.app = app
        self.client = client
        self.host = host
        self.scheme = scheme
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: dict[str, str] | None = None,
        host: str | None = None,
        client: tuple[str, int] | None = None,
        chunks: list[bytes] | None = None,
    ) -> Response:
        return asyncio.run(
            self._call(method, path, headers or {}, json, content, params, host, client, chunks)
        )

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Response:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> Response:
        return self.request("DELETE", path, **kw)

    async def _call(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        json: Any,
        content: bytes | None,
        params: dict[str, str] | None,
        host: str | None,
        client: tuple[str, int] | None,
        chunks: list[bytes] | None,
    ) -> Response:
        hdrs = {k.lower(): v for k, v in headers.items()}
        body = content or b""
        if json is not None:
            body = jsonlib.dumps(json).encode()
            hdrs.setdefault("content-type", "application/json")
        hdrs.setdefault("host", host or self.host)
        if chunks is None:
            hdrs.setdefault("content-length", str(len(body)))
        if self.cookies and "cookie" not in hdrs:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        query = urlencode(params or {}).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": self.scheme,
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in hdrs.items()],
            "client": client or self.client,
            "server": ("testserver", 80),
            "root_path": "",
        }
        pending = list(chunks) if chunks is not None else [body]
        sent_all = False

        async def receive() -> dict[str, Any]:
            nonlocal sent_all
            if pending:
                piece = pending.pop(0)
                return {"type": "http.request", "body": piece, "more_body": bool(pending)}
            if not sent_all:
                sent_all = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        status = 0
        resp_headers: list[tuple[bytes, bytes]] = []
        parts: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, resp_headers
            if message["type"] == "http.response.start":
                status = message["status"]
                resp_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                parts.append(message.get("body", b""))

        await self.app(scope, receive, send)
        response = Response(status, resp_headers, b"".join(parts))
        for raw in response.header_list("set-cookie"):
            jar = SimpleCookie()
            jar.load(raw)
            for name, morsel in jar.items():
                if morsel.value == "" or morsel["max-age"] == "0":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        return response
