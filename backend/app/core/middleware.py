"""Pure-ASGI middleware: edge (request id, security headers, error containment), host check, body limit.

Order matters (see ``app.main.create_app``): ``EdgeMiddleware`` is outermost so that *every*
response, including ones produced by the inner middleware and unexpected-exception 500s, carries a
request id and the security headers. Unexpected exceptions are logged (redacted) and turned into a
generic 500; internals are never returned to the client.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, MutableMapping
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import errors
from app.core.logging import request_id_var

log = logging.getLogger("orbitnest.http")

_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"content-security-policy", CSP.encode("ascii")),
)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _send_error(
    scope: Scope, receive: Receive, send: Send, status: int, code: str, message: str
) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    await errors.error_response(status, code, message)(scope, receive, send)


class EdgeMiddleware:
    def __init__(self, app: ASGIApp, *, trusted_proxy: bool = False) -> None:
        self.app = app
        self.trusted_proxy = trusted_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        supplied = _header(scope, b"x-request-id")
        request_id = supplied if supplied and _REQUEST_ID_OK.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        secure = scope.get("scheme") in ("https", "wss")
        if self.trusted_proxy and _header(scope, b"x-forwarded-proto") == "https":
            secure = True
        is_api = scope.get("path", "").startswith("/api/")

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.append((b"x-request-id", request_id.encode("ascii")))
                for key, value in _STATIC_HEADERS:
                    if key not in present:
                        headers.append((key, value))
                if is_api and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                if secure:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error", extra={"path": scope.get("path"), "method": scope.get("method")})
            if not started:
                await _send_error(
                    scope, receive, send_wrapper, 500, errors.INTERNAL_ERROR, "An internal error occurred."
                )
        finally:
            request_id_var.reset(token)


class HostCheckMiddleware:
    """Reject unexpected Host headers (DNS-rebinding defence for a loopback-bound service)."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed = {h.lower() for h in allowed_hosts}

    @staticmethod
    def _hostname(value: str) -> str:
        value = value.strip().lower()
        if value.startswith("["):  # IPv6 literal, optionally with :port
            end = value.find("]")
            return value[: end + 1] if end != -1 else value
        return value.rsplit(":", 1)[0] if value.count(":") == 1 else value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = _header(scope, b"host")
            name = self._hostname(host) if host else ""
            if not name or (name not in self.allowed and name.strip("[]") not in self.allowed):
                await _send_error(scope, receive, send, 400, errors.INVALID_HOST, "Invalid host header.")
                return
        await self.app(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject oversized bodies by Content-Length and while streaming."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await _send_error(
                    scope, receive, send, 413, errors.PAYLOAD_TOO_LARGE, "Request body is too large."
                )
                return

        seen = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_error(
                    scope, receive, send, 413, errors.PAYLOAD_TOO_LARGE, "Request body is too large."
                )


def is_loopback_client(scope_or_client: MutableMapping[str, Any] | tuple[str, int] | None) -> bool:
    client = scope_or_client.get("client") if isinstance(scope_or_client, dict) else scope_or_client
    return bool(client) and client[0] in {"127.0.0.1", "::1"}  # type: ignore[index]
