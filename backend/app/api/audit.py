from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth.deps import authenticate
from app.auth.policy import Perm
from app.core import errors
from app.services import audit


def list_audit_events(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.AUDIT_READ)
    q = request.query_params
    outcome = q.get("outcome")
    if outcome is not None and outcome not in audit.OUTCOMES:
        raise errors.AppError(errors.VALIDATION_ERROR, "outcome must be success, failure or denied.", 422)
    try:
        limit = int(q.get("limit", "50"))
    except ValueError:
        raise errors.AppError(errors.VALIDATION_ERROR, "limit must be an integer.", 422) from None
    try:
        events, next_cursor = audit.list_events(
            request.app.state.db,
            limit=limit,
            cursor=q.get("cursor"),
            action_prefix=(q.get("action") or None),
            outcome=outcome,
            actor_user_id=(q.get("actor") or None),
        )
    except ValueError:
        raise errors.AppError(errors.VALIDATION_ERROR, "cursor is invalid.", 422) from None
    return JSONResponse({"items": events, "next_cursor": next_cursor})


routes = [Route("/audit-events", list_audit_events, methods=["GET"])]
