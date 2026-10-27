"""Consistent error objects with stable error codes.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": [...]?}}

Messages are safe to show to users: they never contain server paths, secrets, or echoed input.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.logging import request_id_var

# Stable codes. Add new ones; never repurpose an existing one.
VALIDATION_ERROR = "validation_error"
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
CONFLICT = "conflict"
RATE_LIMITED = "rate_limited"
PAYLOAD_TOO_LARGE = "payload_too_large"
INVALID_HOST = "invalid_host"
NOT_READY = "not_ready"
INVALID_CREDENTIALS = "invalid_credentials"
MFA_REQUIRED = "mfa_required"
CSRF_FAILED = "csrf_failed"
SETUP_UNAVAILABLE = "setup_unavailable"
INVALID_SETUP_TOKEN = "invalid_setup_token"  # noqa: S105 - error code, not a credential
INVALID_RESET_TOKEN = "invalid_reset_token"  # noqa: S105 - error code, not a credential
LAST_ADMIN = "last_admin"
TEMPLATE_UNAVAILABLE = "template_unavailable"
QUOTA_EXCEEDED = "quota_exceeded"
SESSION_LIMIT = "session_limit"
WORKSPACE_BUSY = "workspace_busy"
WORKSPACE_DELETED = "workspace_deleted"
CONFIRMATION_REQUIRED = "confirmation_required"
NOT_SUPPORTED = "not_supported"
INTERNAL_ERROR = "internal_error"
HTTP_ERROR = "http_error"


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.headers = headers


def error_body(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}
    if details:
        body["error"]["details"] = details
    return body


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status_code, headers=headers)


def validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    """Field locations and reasons only; the offending input value is never echoed."""
    return [
        {"field": ".".join(str(p) for p in err["loc"]) or "body", "reason": err["msg"], "type": err["type"]}
        for err in exc.errors(include_input=False, include_url=False, include_context=False)
    ]


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        return error_response(404, NOT_FOUND, "Resource not found.")
    if exc.status_code == 405:
        return error_response(405, METHOD_NOT_ALLOWED, "Method not allowed.", headers=dict(exc.headers or {}))
    return error_response(exc.status_code, HTTP_ERROR, "Request could not be processed.")


async def parse_json_body(request: Request, model: type[Any]) -> Any:
    """Parse and validate a JSON body against a Pydantic model. Enforces content type."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise AppError(VALIDATION_ERROR, "Content-Type must be application/json.", 415)
    raw = await request.body()
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise AppError(VALIDATION_ERROR, "Request body is invalid.", 422, validation_details(exc)) from None
