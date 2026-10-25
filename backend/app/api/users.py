"""Admin-only user management. Users are deactivated, never deleted, to preserve audit integrity."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth.deps import authenticate, request_context
from app.auth.policy import Perm
from app.auth.service import AuthService
from app.core import errors
from app.core.errors import parse_json_body
from app.schemas.auth import CreateUserRequest, UpdateUserRequest


def _service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth
    return service


def _int_param(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise errors.AppError(errors.VALIDATION_ERROR, f"{name} must be an integer.", 422) from None
    return max(low, min(value, high))


def list_users(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.USERS_MANAGE)
    limit = _int_param(request, "limit", 100, 1, 200)
    offset = _int_param(request, "offset", 0, 0, 1_000_000)
    users = _service(request).list_users(limit=limit, offset=offset)
    return JSONResponse({"items": [u.to_public() for u in users], "limit": limit, "offset": offset})


async def create_user(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.USERS_MANAGE)
    data = await parse_json_body(request, CreateUserRequest)
    service = _service(request)
    user = await run_in_threadpool(
        lambda: service.create_user(
            auth.user,
            username=data.username,
            password=data.password,
            role=data.role,
            ctx=request_context(request),
        )
    )
    return JSONResponse(user.to_public(), status_code=201)


async def update_user(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.USERS_MANAGE)
    data = await parse_json_body(request, UpdateUserRequest)
    service = _service(request)
    user = await run_in_threadpool(
        lambda: service.update_user(
            auth.user,
            request.path_params["user_id"],
            role=data.role,
            is_active=data.is_active,
            ctx=request_context(request),
        )
    )
    return JSONResponse(user.to_public())


def issue_password_reset(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.USERS_MANAGE)
    token, expires_at = _service(request).issue_reset(
        auth.user, request.path_params["user_id"], request_context(request)
    )
    # The token is shown exactly once; only its hash is stored.
    return JSONResponse({"token": token, "expires_at": expires_at}, status_code=201)


routes = [
    Route("/users", list_users, methods=["GET"]),
    Route("/users", create_user, methods=["POST"]),
    Route("/users/{user_id}", update_user, methods=["PATCH"]),
    Route("/users/{user_id}/password-reset", issue_password_reset, methods=["POST"]),
]
