"""Request-level authentication: cookie handling, Origin/CSRF checks, permission enforcement.

Every protected handler calls :func:`authenticate`. Unsafe methods (anything but GET/HEAD/OPTIONS)
automatically require a valid ``X-CSRF-Token`` *and* an acceptable Origin. Authorization is decided
here and in services, never by the UI.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from app.auth.policy import has_permission
from app.auth.service import AuthContext, AuthService
from app.core import errors
from app.core.config import Settings
from app.services import audit
from app.services.audit import RequestContext

log = logging.getLogger("orbitnest.auth")

COOKIE_NAME = "orbitnest_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def client_ip(request: HTTPConnection) -> str | None:
    """Peer address. ``X-Forwarded-For`` is honoured only when a trusted proxy is configured,
    and then only the rightmost entry (the one our own proxy appended)."""
    settings: Settings = request.app.state.settings
    if settings.trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            last = forwarded.split(",")[-1].strip()
            if last:
                return last[:64]
    return request.client.host if request.client else None


def request_context(request: HTTPConnection) -> RequestContext:
    return RequestContext(
        ip=client_ip(request),
        request_id=getattr(request.state, "request_id", None)
        or request.scope.get("state", {}).get("request_id"),
        user_agent=request.headers.get("user-agent"),
    )


def cookie_is_secure(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    if settings.cookie_secure == "always":
        return True
    if settings.cookie_secure == "never":
        return False
    if request.url.scheme == "https":
        return True
    return settings.trusted_proxy and request.headers.get("x-forwarded-proto") == "https"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    settings: Settings = request.app.state.settings
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=cookie_is_secure(request),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, secure=cookie_is_secure(request), samesite="lax"
    )


def origin_acceptable(request: HTTPConnection) -> bool:
    """Same-origin check for unsafe requests.

    Browsers always send Origin on cross-origin POSTs; if it is absent we fall back to
    Sec-Fetch-Site. Non-browser clients send neither and are allowed (they cannot be CSRF'd).
    """
    settings: Settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site") in (None, "same-origin", "none")
    if origin == "null":
        return False
    parts = urlsplit(origin)
    host_header = (request.headers.get("host") or "").lower()
    if parts.netloc.lower() == host_header:
        return True
    allowed = set(settings.cors_origins)
    if settings.public_url:
        public = urlsplit(settings.public_url)
        allowed.add(f"{public.scheme}://{public.netloc}")
    return origin.rstrip("/") in allowed


def require_same_origin(request: Request) -> None:
    """For unauthenticated unsafe endpoints (login, setup, reset): blocks cross-site form posts."""
    if request.method not in SAFE_METHODS and not origin_acceptable(request):
        raise errors.AppError(errors.CSRF_FAILED, "Cross-origin request rejected.", 403)


def authenticate(request: Request, *, permission: str | None = None, csrf: bool | None = None) -> AuthContext:
    """Return the authenticated context or raise 401/403.

    ``csrf=None`` means "required for unsafe methods"; pass ``False`` only for endpoints that
    must work without it (there are none today).
    """
    service: AuthService = request.app.state.auth
    token = request.cookies.get(COOKIE_NAME)
    auth = service.resolve_session(token) if token else None
    if auth is None:
        raise errors.AppError(errors.UNAUTHORIZED, "Authentication required.", 401)

    ctx = request_context(request)
    needs_csrf = request.method not in SAFE_METHODS if csrf is None else csrf
    if needs_csrf:
        supplied = request.headers.get(CSRF_HEADER)
        if not origin_acceptable(request) or not service.verify_csrf(auth.session_id, supplied):
            audit.record(
                service.db,
                action="authz.csrf_rejected",
                outcome="denied",
                actor_user_id=auth.user.id,
                ctx=ctx,
                details={"method": request.method, "path": request.url.path},
            )
            raise errors.AppError(errors.CSRF_FAILED, "CSRF validation failed.", 403)

    if permission is not None and not has_permission(auth.user.role, permission):
        audit.record(
            service.db,
            action="authz.denied",
            outcome="denied",
            actor_user_id=auth.user.id,
            ctx=ctx,
            details={"permission": permission, "method": request.method, "path": request.url.path},
        )
        raise errors.AppError(errors.FORBIDDEN, "You do not have permission to do that.", 403)
    return auth


def authenticate_ws(conn: HTTPConnection, *, permission: str) -> AuthContext:
    """Authenticate a WebSocket handshake: session cookie, same-origin check, permission.

    Browsers cannot attach custom headers to WebSockets, so CSRF tokens are not used here; the
    Origin check (plus SameSite=Lax cookies) is what stops cross-site WebSocket hijacking.
    """
    service: AuthService = conn.app.state.auth
    token = conn.cookies.get(COOKIE_NAME)
    auth = service.resolve_session(token) if token else None
    if auth is None:
        raise errors.AppError(errors.UNAUTHORIZED, "Authentication required.", 401)
    ctx = request_context(conn)
    if not origin_acceptable(conn):
        audit.record(
            service.db,
            action="authz.ws_origin_rejected",
            outcome="denied",
            actor_user_id=auth.user.id,
            ctx=ctx,
            details={"path": conn.url.path},
        )
        raise errors.AppError(errors.CSRF_FAILED, "Cross-origin request rejected.", 403)
    if not has_permission(auth.user.role, permission):
        audit.record(
            service.db,
            action="authz.denied",
            outcome="denied",
            actor_user_id=auth.user.id,
            ctx=ctx,
            details={"permission": permission, "path": conn.url.path},
        )
        raise errors.AppError(errors.FORBIDDEN, "You do not have permission to do that.", 403)
    return auth
