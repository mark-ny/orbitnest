"""Health, readiness and capability routes (``/api/v1``).

``/health`` and ``/ready`` are intentionally public and minimal. Capabilities expose host details,
so they require an authenticated session.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import __version__
from app.auth.deps import authenticate
from app.auth.policy import Perm
from app.db import migration_status
from app.services import sysinfo


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


def capabilities(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.SYSTEM_READ)
    report = request.app.state.detect_capabilities()
    return JSONResponse(report.model_dump(mode="json"))


async def system_report(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.TEMPLATES_MANAGE)  # administrators only
    return JSONResponse(await run_in_threadpool(sysinfo.build_report, request.app))


async def update_report(request: Request) -> JSONResponse:
    authenticate(request, permission=Perm.TEMPLATES_MANAGE)
    return JSONResponse(await run_in_threadpool(sysinfo.update_status))


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/ready", ready, methods=["GET"]),
    Route("/system/capabilities", capabilities, methods=["GET"]),
    Route("/system/report", system_report, methods=["GET"]),
    Route("/system/update", update_report, methods=["GET"]),
]

PUBLIC_ROUTES = frozenset({("GET", "/api/v1/health"), ("GET", "/api/v1/ready")})
