"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from app.api import system
from app.core import errors
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.middleware import BodyLimitMiddleware, EdgeMiddleware, HostCheckMiddleware
from app.core.platform import Capabilities, detect_capabilities
from app.db import Database, migrate

log = logging.getLogger("orbitnest")

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    capability_detector: Callable[[], Capabilities] | None = None,
    run_migrations: bool = True,
) -> Starlette:
    settings = (settings or Settings.from_env()).with_resolved_secret()
    configure_logging(settings)

    db = Database(settings.db_path)
    if run_migrations:
        applied = migrate(db)
        if applied:
            log.info("applied migrations", extra={"versions": applied})

    middleware = [
        Middleware(EdgeMiddleware, trusted_proxy=settings.trusted_proxy),
        Middleware(HostCheckMiddleware, allowed_hosts=settings.effective_allowed_hosts),
        Middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes),
    ]
    if settings.cors_origins:
        # Explicit origins only; credentials are allowed solely because wildcards are rejected.
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "PATCH", "DELETE"],
                allow_headers=["content-type", "x-csrf-token", "x-request-id"],
            )
        )

    app = Starlette(
        routes=[Mount(API_PREFIX, routes=system.routes)],
        middleware=middleware,
        exception_handlers={
            errors.AppError: errors.app_error_handler,
            HTTPException: errors.http_exception_handler,
        },
    )
    app.state.settings = settings
    app.state.db = db
    app.state.detect_capabilities = capability_detector or (lambda: detect_capabilities(settings.data_dir))
    return app
