"""Authentication routes. Hashing and DB work run in a threadpool so they never block the loop."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.auth.deps import (
    authenticate,
    clear_session_cookie,
    request_context,
    require_same_origin,
    set_session_cookie,
)
from app.auth.service import AuthService
from app.core.errors import parse_json_body
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MfaBeginRequest,
    MfaConfirmRequest,
    MfaDisableRequest,
    ResetRedeemRequest,
    SetupRequest,
)


def _service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth
    return service


def _session_payload(service: AuthService, user, session_id: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {"user": user.to_public(), "csrf_token": service.csrf_token(session_id)}


async def setup_status(request: Request) -> JSONResponse:
    service = _service(request)
    required = await run_in_threadpool(service.setup_required)
    body: dict[str, object] = {"setup_required": required}
    if required:
        # Pre-setup only: a coarse runtime hint for the first-run page. Cached; no host detail.
        body["runtime_mode"] = (await run_in_threadpool(request.app.state.detect_capabilities)).mode
    return JSONResponse(body)


async def setup(request: Request) -> JSONResponse:
    require_same_origin(request)
    data = await parse_json_body(request, SetupRequest)
    service = _service(request)
    user, token, session_id = await run_in_threadpool(
        lambda: service.setup_admin(
            username=data.username,
            password=data.password,
            supplied_token=data.setup_token,
            app_url=data.app_url,
            ctx=request_context(request),
        )
    )
    response = JSONResponse(_session_payload(service, user, session_id), status_code=201)
    set_session_cookie(response, request, token)
    return response


async def login(request: Request) -> JSONResponse:
    require_same_origin(request)
    data = await parse_json_body(request, LoginRequest)
    service = _service(request)
    ctx = request_context(request)
    user, token, session_id = await run_in_threadpool(
        lambda: service.login(username=data.username, password=data.password, ctx=ctx, code=data.code)
    )
    response = JSONResponse(_session_payload(service, user, session_id))
    set_session_cookie(response, request, token)
    return response


async def logout(request: Request) -> Response:
    auth = authenticate(request)
    service = _service(request)
    await run_in_threadpool(service.logout, auth, request_context(request))
    response = Response(status_code=204)
    clear_session_cookie(response, request)
    return response


def current_session(request: Request) -> JSONResponse:
    auth = authenticate(request)
    return JSONResponse(_session_payload(_service(request), auth.user, auth.session_id))


async def change_password(request: Request) -> Response:
    auth = authenticate(request)
    data = await parse_json_body(request, ChangePasswordRequest)
    service = _service(request)
    await run_in_threadpool(
        service.change_password, auth, data.current_password, data.new_password, request_context(request)
    )
    return Response(status_code=204)


def list_sessions(request: Request) -> JSONResponse:
    auth = authenticate(request)
    sessions = _service(request).list_sessions(auth.user.id, auth.session_id)
    return JSONResponse({"items": sessions})


def revoke_session(request: Request) -> Response:
    auth = authenticate(request)
    _service(request).revoke_session_for(auth, request.path_params["session_id"], request_context(request))
    return Response(status_code=204)


async def reset_password(request: Request) -> Response:
    require_same_origin(request)
    data = await parse_json_body(request, ResetRedeemRequest)
    service = _service(request)
    await run_in_threadpool(
        lambda: service.redeem_reset(
            token=data.token, new_password=data.new_password, ctx=request_context(request)
        )
    )
    return Response(status_code=204)


def mfa_status(request: Request) -> JSONResponse:
    auth = authenticate(request)
    return JSONResponse(_service(request).mfa_status(auth.user.id))


async def mfa_begin(request: Request) -> JSONResponse:
    auth = authenticate(request)
    data = await parse_json_body(request, MfaBeginRequest)
    result = await run_in_threadpool(
        _service(request).mfa_begin, auth, data.password, request_context(request)
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def mfa_confirm(request: Request) -> JSONResponse:
    auth = authenticate(request)
    data = await parse_json_body(request, MfaConfirmRequest)
    codes = await run_in_threadpool(_service(request).mfa_confirm, auth, data.code, request_context(request))
    return JSONResponse({"recovery_codes": codes}, headers={"Cache-Control": "no-store"})


async def mfa_disable(request: Request) -> Response:
    auth = authenticate(request)
    data = await parse_json_body(request, MfaDisableRequest)
    await run_in_threadpool(
        _service(request).mfa_disable, auth, data.password, data.code, request_context(request)
    )
    return Response(status_code=204)


routes = [
    Route("/auth/setup-status", setup_status, methods=["GET"]),
    Route("/auth/setup", setup, methods=["POST"]),
    Route("/auth/login", login, methods=["POST"]),
    Route("/auth/logout", logout, methods=["POST"]),
    Route("/auth/session", current_session, methods=["GET"]),
    Route("/auth/password", change_password, methods=["POST"]),
    Route("/auth/sessions", list_sessions, methods=["GET"]),
    Route("/auth/sessions/{session_id}", revoke_session, methods=["DELETE"]),
    Route("/auth/reset-password", reset_password, methods=["POST"]),
    Route("/auth/mfa", mfa_status, methods=["GET"]),
    Route("/auth/mfa/begin", mfa_begin, methods=["POST"]),
    Route("/auth/mfa/confirm", mfa_confirm, methods=["POST"]),
    Route("/auth/mfa/disable", mfa_disable, methods=["POST"]),
]

# Routes reachable without a session. tests/test_route_guard.py fails if any other route answers
# an anonymous request, so adding a public route requires touching this list deliberately.
PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/api/v1/auth/setup-status"),
        ("POST", "/api/v1/auth/setup"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/reset-password"),
    }
)
