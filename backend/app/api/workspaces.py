"""Template, workspace and policy routes (``/api/v1``). Authorization lives in the service."""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.auth.deps import authenticate, request_context
from app.auth.policy import Perm
from app.core import errors
from app.core.errors import parse_json_body
from app.schemas.workspaces import (
    CreateWorkspaceRequest,
    CustomTemplateRequest,
    PolicyRequest,
    ResetWorkspaceRequest,
    TemplateEnabledRequest,
    UpdateWorkspaceRequest,
)
from app.services import audit
from app.services import policy as policy_mod
from app.services.templates import Template, TemplateError, evaluate
from app.services.workspace_store import Workspace
from app.services.workspaces import WorkspaceService

_TEMPLATE_PUBLIC = (
    "id", "name", "description", "category", "access", "default_profile", "max_profile",
    "default_persistence", "default_idle_minutes", "network_policy", "allowed_networks",
    "authorized_use_only", "deprecated", "deprecation_note", "update_policy", "enabled",
    "disabled_reason", "notes", "builtin", "local_supported",
)  # fmt: skip


def _svc(request: Request) -> WorkspaceService:
    svc: WorkspaceService = request.app.state.workspaces
    return svc


def workspace_json(ws: Workspace, *, include_owner: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": ws.id,
        "name": ws.name,
        "template_id": ws.template_id,
        "runtime": ws.runtime,
        # "none" means the workload is an ordinary process on the host (Termux), NOT a container.
        "isolation": "none" if ws.runtime == "local" else "container",
        "status": ws.status,
        "status_reason": ws.status_reason,
        "persistence": ws.persistence,
        "resource_profile": ws.resource_profile,
        "limits": {
            "cpu_millis": ws.cpu_millis,
            "memory_mb": ws.memory_mb,
            "pids": ws.pids_limit,
            "storage_mb": ws.storage_quota_mb,
        },
        "idle_timeout_minutes": ws.idle_timeout_minutes,
        "network_policy": ws.network_policy,
        "labels": ws.labels,
        "data": ws.volume_state,
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
        "started_at": ws.started_at,
        "last_activity_at": ws.last_activity_at,
        "health": {"checked_at": ws.last_health_at, "ok": ws.last_health_ok},
    }
    if include_owner:
        body["owner"] = {"id": ws.owner_id, "username": ws.owner_username}
    return body


def _int(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return max(low, min(int(raw), high))
    except ValueError:
        raise errors.AppError(errors.VALIDATION_ERROR, f"{name} must be an integer.", 422) from None


def _flag(request: Request, name: str) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return False
    if raw.lower() in ("1", "true"):
        return True
    if raw.lower() in ("0", "false"):
        return False
    raise errors.AppError(errors.VALIDATION_ERROR, f"{name} must be true or false.", 422)


def _template_json(request: Request, tpl: Template) -> dict[str, Any]:
    caps = request.app.state.detect_capabilities()
    avail = evaluate(tpl, caps)
    data = {k: getattr(tpl, k) for k in _TEMPLATE_PUBLIC}
    data["availability"] = avail.to_dict()
    return data


# -- templates ------------------------------------------------------------------------------
async def list_templates(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.WORKSPACE_READ)
    registry = request.app.state.templates
    items = await run_in_threadpool(lambda: [_template_json(request, t) for t in registry.list()])
    return JSONResponse({"items": items})


async def get_template(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.WORKSPACE_READ)
    tpl = await run_in_threadpool(request.app.state.templates.get, request.path_params["template_id"])
    if tpl is None:
        raise errors.AppError(errors.NOT_FOUND, "Template not found.", 404)
    return JSONResponse(await run_in_threadpool(_template_json, request, tpl))


async def create_template(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.TEMPLATES_MANAGE)
    data = await parse_json_body(request, CustomTemplateRequest)
    registry = request.app.state.templates
    try:
        tpl = await run_in_threadpool(registry.create_custom, auth.user.id, data.model_dump())
    except TemplateError as exc:
        raise errors.AppError(errors.VALIDATION_ERROR, str(exc), 422) from None
    audit.record(
        request.app.state.db,
        action="template.create",
        outcome="success",
        actor_user_id=auth.user.id,
        target_type="template",
        target_id=tpl.id,
        ctx=request_context(request),
    )
    return JSONResponse(_template_json(request, tpl), status_code=201)


async def update_template(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.TEMPLATES_MANAGE)
    data = await parse_json_body(request, TemplateEnabledRequest)
    tid = request.path_params["template_id"]
    if not tid.startswith("custom-"):
        raise errors.AppError(errors.FORBIDDEN, "Built-in templates cannot be modified.", 403)
    tpl = await run_in_threadpool(request.app.state.templates.set_enabled, tid, data.enabled)
    if tpl is None:
        raise errors.AppError(errors.NOT_FOUND, "Template not found.", 404)
    audit.record(
        request.app.state.db,
        action="template.update",
        outcome="success",
        actor_user_id=auth.user.id,
        target_type="template",
        target_id=tid,
        ctx=request_context(request),
        details={"enabled": data.enabled},
    )
    return JSONResponse(_template_json(request, tpl))


async def delete_template(request: Request) -> Response:
    auth = authenticate(request, permission=Perm.TEMPLATES_MANAGE)
    tid = request.path_params["template_id"]
    if not tid.startswith("custom-"):
        raise errors.AppError(errors.FORBIDDEN, "Built-in templates cannot be deleted.", 403)
    try:
        removed = await run_in_threadpool(request.app.state.templates.delete_custom, tid)
    except TemplateError as exc:
        raise errors.AppError(errors.CONFLICT, str(exc), 409) from None
    if not removed:
        raise errors.AppError(errors.NOT_FOUND, "Template not found.", 404)
    audit.record(
        request.app.state.db,
        action="template.delete",
        outcome="success",
        actor_user_id=auth.user.id,
        target_type="template",
        target_id=tid,
        ctx=request_context(request),
    )
    return Response(status_code=204)


# -- workspaces -----------------------------------------------------------------------------
async def list_workspaces(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_READ)
    q = request.query_params
    limit, offset = _int(request, "limit", 50, 1, 200), _int(request, "offset", 0, 0, 1_000_000)
    all_users = _flag(request, "all")
    items, total = await run_in_threadpool(
        lambda: _svc(request).list(
            auth.user,
            all_users=all_users,
            status=q.get("status") or None,
            template_id=q.get("template") or None,
            query=(q.get("q") or None)[:64] if q.get("q") else None,
            limit=limit,
            offset=offset,
        )
    )
    return JSONResponse(
        {
            "items": [workspace_json(w, include_owner=all_users) for w in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


async def create_workspace(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    data = await parse_json_body(request, CreateWorkspaceRequest)
    key = request.headers.get("idempotency-key")
    ws, created = await run_in_threadpool(
        lambda: _svc(request).create(
            auth.user, idempotency_key=key, ctx=request_context(request), **data.model_dump()
        )
    )
    return JSONResponse(workspace_json(ws), status_code=202 if created else 200)


async def get_workspace(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_READ)
    ws = await run_in_threadpool(
        _svc(request).get, auth.user, request.path_params["workspace_id"], request_context(request)
    )
    return JSONResponse(workspace_json(ws, include_owner=auth.user.role == "admin"))


async def update_workspace(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    data = await parse_json_body(request, UpdateWorkspaceRequest)
    ws = await run_in_threadpool(
        lambda: _svc(request).update(
            auth.user,
            request.path_params["workspace_id"],
            ctx=request_context(request),
            **data.model_dump(exclude_unset=True),
        )
    )
    return JSONResponse(workspace_json(ws))


def _action(name: str):
    async def handler(request: Request) -> JSONResponse:
        auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
        method = getattr(_svc(request), name)
        ws = await run_in_threadpool(
            method, auth.user, request.path_params["workspace_id"], request_context(request)
        )
        return JSONResponse(workspace_json(ws), status_code=200 if name == "touch_activity" else 202)

    handler.__name__ = f"{name}_workspace"
    return handler


async def reset_workspace(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    raw = await request.body()
    data = await parse_json_body(request, ResetWorkspaceRequest) if raw else ResetWorkspaceRequest()
    ws = await run_in_threadpool(
        lambda: _svc(request).reset(
            auth.user,
            request.path_params["workspace_id"],
            wipe_data=data.wipe_data,
            confirm_name=data.confirm_name,
            ctx=request_context(request),
        )
    )
    return JSONResponse(workspace_json(ws), status_code=202)


async def delete_workspace(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    delete_data = _flag(request, "delete_data")
    confirm = request.query_params.get("confirm_name")
    ws = await run_in_threadpool(
        lambda: _svc(request).delete(
            auth.user,
            request.path_params["workspace_id"],
            delete_data=delete_data,
            confirm_name=confirm,
            ctx=request_context(request),
        )
    )
    return JSONResponse(workspace_json(ws), status_code=202)


async def workspace_events(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_READ)
    items = await run_in_threadpool(
        _svc(request).events, auth.user, request.path_params["workspace_id"], request_context(request)
    )
    return JSONResponse({"items": items})


# -- policy ---------------------------------------------------------------------------------
async def get_policy(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.SETTINGS_MANAGE)
    pol = await run_in_threadpool(policy_mod.load, request.app.state.db)
    return JSONResponse(pol.to_dict())


async def update_policy(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.SETTINGS_MANAGE)
    data = await parse_json_body(request, PolicyRequest)
    changes = data.changes()
    if not changes:
        raise errors.AppError(errors.VALIDATION_ERROR, "Provide at least one setting.", 422)
    pol = await run_in_threadpool(policy_mod.update, request.app.state.db, changes)
    audit.record(
        request.app.state.db,
        action="settings.policy_update",
        outcome="success",
        actor_user_id=auth.user.id,
        ctx=request_context(request),
        details=changes,
    )
    return JSONResponse(pol.to_dict())


routes = [
    Route("/templates", list_templates, methods=["GET"]),
    Route("/templates", create_template, methods=["POST"]),
    Route("/templates/{template_id}", get_template, methods=["GET"]),
    Route("/templates/{template_id}", update_template, methods=["PATCH"]),
    Route("/templates/{template_id}", delete_template, methods=["DELETE"]),
    Route("/workspaces", list_workspaces, methods=["GET"]),
    Route("/workspaces", create_workspace, methods=["POST"]),
    Route("/workspaces/{workspace_id}", get_workspace, methods=["GET"]),
    Route("/workspaces/{workspace_id}", update_workspace, methods=["PATCH"]),
    Route("/workspaces/{workspace_id}", delete_workspace, methods=["DELETE"]),
    Route("/workspaces/{workspace_id}/start", _action("start"), methods=["POST"]),
    Route("/workspaces/{workspace_id}/stop", _action("stop"), methods=["POST"]),
    Route("/workspaces/{workspace_id}/restart", _action("restart"), methods=["POST"]),
    Route("/workspaces/{workspace_id}/reset", reset_workspace, methods=["POST"]),
    Route("/workspaces/{workspace_id}/activity", _action("touch_activity"), methods=["POST"]),
    Route("/workspaces/{workspace_id}/events", workspace_events, methods=["GET"]),
    Route("/settings/policy", get_policy, methods=["GET"]),
    Route("/settings/policy", update_policy, methods=["PATCH"]),
]
