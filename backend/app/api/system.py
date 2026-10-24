"""Health, readiness and capability routes (``/api/v1``).

``/health`` and ``/ready`` are intentionally public and minimal. ``/system/capabilities`` exposes
host details, so until authentication exists (Phase 2) it answers loopback clients only. Phase 2
replaces ``require_local_client`` with real authentication and role checks.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import __version__
from app.core import errors
from app.core.middleware import is_loopback_client
from app.db import migration_status


def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


def ready(request: Request) -> JSONResponse:
    db = request.app.state.db
    db_ok = db.ping()
    migrations_ok = False
    if db_ok:
        migrations_ok = migration_status(db).up_to_date
    checks = {"database": db_ok, "migrations": migrations_ok}
    if all(checks.values()):
        return JSONResponse({"status": "ready", "checks": checks})
    return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)


def require_local_client(request: Request) -> None:
    if not is_loopback_client(request.scope):
        raise errors.AppError(errors.FORBIDDEN, "This endpoint is only available locally.", 403)


def capabilities(request: Request) -> JSONResponse:
    require_local_client(request)
    report = request.app.state.detect_capabilities()
    return JSONResponse(report.model_dump(mode="json"))


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/ready", ready, methods=["GET"]),
    Route("/system/capabilities", capabilities, methods=["GET"]),
]
