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

_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}\Z")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

_HOST_OK = re.compile(r"^[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?$|^\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?$")


def csp_for(host: str | None, secure: bool) -> bytes:
    """CSP with an explicit same-host WebSocket origin (older engines do not treat 'self' as ws:)."""
    if not host or not _HOST_OK.match(host):
        return CSP.encode("ascii")
    scheme = "wss" if secure else "ws"
    return CSP.replace("connect-src 'self'", f"connect-src 'self' {scheme}://{host}").encode("ascii")


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
                    if key == b"content-security-policy":
                        value = csp_for(_header(scope, b"host"), secure)
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
        except Exception as exc:
            if scope["type"] == "websocket" and _is_client_gone(exc):
                # A browser tab closed mid-session. Expected, and not worth a traceback.
                log.info("websocket client disconnected", extra={"path": scope.get("path")})
                return
            log.exception("unhandled error", extra={"path": scope.get("path"), "method": scope.get("method")})
            if not started:
                await _send_error(
                    scope, receive, send_wrapper, 500, errors.INTERNAL_ERROR, "An internal error occurred."
                )
        finally:
            request_id_var.reset(token)


def _is_client_gone(exc: BaseException | None) -> bool:
    """True when the error (or what it was raised while handling) is just the peer going away."""
    seen = 0
    while exc is not None and seen < 5:
        if type(exc).__name__ in {"WebSocketDisconnect", "ClientDisconnected", "ConnectionResetError"}:
            return True
        exc = exc.__context__ or exc.__cause__
        seen += 1
    return False


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

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        route_limits: list[tuple[str, re.Pattern[str], int]] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.route_limits = route_limits or []  # (method, path regex, larger limit) for uploads

    def _limit_for(self, scope: Scope) -> int:
        for method, pattern, limit in self.route_limits:
            if scope.get("method") == method and pattern.match(scope.get("path", "")):
                return limit
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope)
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
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
                if seen > limit:
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
