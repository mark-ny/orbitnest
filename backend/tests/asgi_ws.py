"""Minimal ASGI WebSocket driver for tests (no websockets/httpx available in the sandbox).

This exercises our ASGI application directly. It does NOT test a real WebSocket server or the
wire protocol (framing, masking, handshake); that needs uvicorn with a websockets backend.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import parse_qsl, urlencode


class WsConn:
    def __init__(
        self,
        app: Any,
        path: str,
        headers: dict[str, str],
        client: tuple[str, int],
        subprotocols: list[str] | None = None,
    ) -> None:
        path, _, query = path.partition("?")
        hdrs = {k.lower(): v for k, v in headers.items()}
        hdrs.setdefault("host", "localhost")
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(parse_qsl(query)).encode(),
            "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
            "client": client,
            "server": ("testserver", 80),
            "subprotocols": list(subprotocols or []),
            "root_path": "",
        }
        self.app = app
        self.to_app: asyncio.Queue = asyncio.Queue()
        self.from_app: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def start(self) -> WsConn:
        await self.to_app.put({"type": "websocket.connect"})

        async def run() -> None:
            await self.app(self.scope, self.to_app.get, self.from_app.put)
            await self.from_app.put({"type": "__done__"})

        self.task = asyncio.ensure_future(run())
        first = await self._next()
        if first["type"] == "websocket.accept":
            self.accepted = True
        elif first["type"] == "websocket.close":
            self.close_code = first.get("code", 1000)
        else:
            raise AssertionError(f"unexpected first message {first}")
        return self

    async def _next(self, timeout: float = 10.0) -> dict:
        return await asyncio.wait_for(self.from_app.get(), timeout)

    async def recv(self, timeout: float = 10.0) -> tuple[str, Any]:
        """('bytes', b..) | ('text', str) | ('close', code) | ('done', None)"""
        msg = await self._next(timeout)
        if msg["type"] == "websocket.send":
            return ("bytes", msg["bytes"]) if msg.get("bytes") is not None else ("text", msg["text"])
        if msg["type"] == "websocket.close":
            self.close_code = msg.get("code", 1000)
            return "close", self.close_code
        return "done", None

    async def recv_until(self, needle: bytes, timeout: float = 10.0) -> bytes:
        buf = b""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while needle not in buf:
            kind, payload = await self.recv(max(0.1, end - loop.time()))
            if kind == "bytes":
                buf += payload
            elif kind in ("close", "done"):
                break
        return buf

    async def send_bytes(self, data: bytes) -> None:
        await self.to_app.put({"type": "websocket.receive", "bytes": data})

    async def send_text(self, text: str) -> None:
        await self.to_app.put({"type": "websocket.receive", "text": text})

    async def disconnect(self) -> None:
        await self.to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self.task:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.task, 10)


def connect(
    app: Any,
    path: str,
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 50000),
    subprotocols: list[str] | None = None,
) -> WsConn:
    hdrs = dict(headers or {})
    if cookies:
        hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return WsConn(app, path, hdrs, client, subprotocols)
