"""Application factory."""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.api import audit as audit_api
from app.api import auth as auth_api
from app.api import files as files_api
from app.api import stream as stream_api
from app.api import system, users
from app.api import terminal as terminal_api
from app.api import workspaces as workspaces_api
from app.auth import setup_token
from app.auth.passwords import PasswordHasher
from app.auth.service import AuthService
from app.core import errors
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.middleware import BodyLimitMiddleware, EdgeMiddleware, HostCheckMiddleware
from app.core.platform import Capabilities, detect_capabilities
from app.db import Database, migrate
from app.runtimes.base import RuntimeAdapter
from app.runtimes.factory import build_runtimes
from app.services.files import FileService
from app.services.image_builder import ImageBuilder
from app.services.jobs import JobRunner, ThreadRunner
from app.services.maintenance import MaintenanceThread
from app.services.streams import StreamService
from app.services.templates import TemplateRegistry
from app.services.terminals import TerminalService
from app.services.workspace_store import WorkspaceStore
from app.services.workspaces import WorkspaceService
from app.storage.manager import StorageManager

log = logging.getLogger("orbitnest")

API_PREFIX = "/api/v1"
CAPABILITY_TTL_SECONDS = 30.0


class CachedDetector:
    """TTL cache so unauthenticated/frequent callers cannot make us spawn subprocesses per request."""

    def __init__(self, detector: Callable[[], Capabilities], ttl: float = CAPABILITY_TTL_SECONDS) -> None:
        self._detector = detector
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: Capabilities | None = None
        self._at = 0.0

    def __call__(self) -> Capabilities:
        with self._lock:
            now = time.monotonic()
            if self._value is None or now - self._at > self._ttl:
                self._value = self._detector()
                self._at = now
            return self._value


WEB_DIR = Path(__file__).parent / "web"


def _frontend_routes() -> list[Mount]:
    """The dependency-free web UI (no build step). Served after the API so /api/* always wins."""
    if not (WEB_DIR / "index.html").is_file():
        return []
    return [Mount("/", app=StaticFiles(directory=WEB_DIR, html=True), name="web")]


def create_app(
    settings: Settings | None = None,
    *,
    capability_detector: Callable[[], Capabilities] | None = None,
    password_hasher: PasswordHasher | None = None,
    run_migrations: bool = True,
    runtimes: dict[str, RuntimeAdapter] | None = None,
    job_runner: JobRunner | None = None,
) -> Starlette:
    settings = (settings or Settings.from_env()).with_resolved_secret()
    configure_logging(settings)

    db = Database(settings.db_path)
    if run_migrations:
        applied = migrate(db)
        if applied:
            log.info("applied migrations", extra={"versions": applied})

    auth_service = AuthService(db, password_hasher or PasswordHasher(), settings)
    if run_migrations:
        if auth_service.setup_required():
            path = setup_token.ensure_setup_token(settings.data_dir)
            log.warning("first-run setup required; read the setup token from the file %s", path.name)
        else:
            setup_token.remove_setup_token(settings.data_dir)

    middleware = [
        Middleware(EdgeMiddleware, trusted_proxy=settings.trusted_proxy),
        Middleware(HostCheckMiddleware, allowed_hosts=settings.effective_allowed_hosts),
        Middleware(
            BodyLimitMiddleware,
            max_bytes=settings.max_request_bytes,
            route_limits=[
                (
                    "PUT",
                    re.compile(r"^/api/v1/workspaces/[0-9A-Z]{26}/files/content\Z"),
                    settings.max_upload_mb * 1024 * 1024,
                )
            ],
        ),
    ]
    if settings.cors_origins:
        # Explicit origins only; credentials are allowed solely because wildcards are rejected.
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                allow_headers=["content-type", "x-csrf-token", "x-request-id"],
            )
        )

    routes = [
        *system.routes,
        *auth_api.routes,
        *users.routes,
        *audit_api.routes,
        *workspaces_api.routes,
        *files_api.routes,
        *terminal_api.routes,
        *stream_api.routes,
    ]
    detector = CachedDetector(capability_detector or (lambda: detect_capabilities(settings.data_dir)))
    registry = TemplateRegistry(db)
    workspace_service = WorkspaceService(
        db=db,
        settings=settings,
        store=WorkspaceStore(db),
        registry=registry,
        storage=StorageManager(settings.workspaces_root),
        # Adapters are chosen lazily from detected capabilities (no docker probing at import time).
        runtimes=(lambda: build_runtimes(settings, detector())) if runtimes is None else runtimes,
        runner=job_runner or ThreadRunner(),
        capabilities=detector,
    )

    def _docker_bin() -> str:
        adapter = workspace_service.adapters().get("docker")
        return getattr(adapter, "docker_bin", "docker")

    image_builder = ImageBuilder(docker_bin=_docker_bin, on_finished=workspace_service._image_cache.clear)
    terminals = TerminalService(workspace_service, settings)
    streams = StreamService(workspace_service, settings)
    maintenance = MaintenanceThread(workspace_service, settings.maintenance_interval_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        maintenance.start()
        try:
            yield
        finally:
            maintenance.stop()
            image_builder.shutdown()
            terminals.close_all()
            streams.close_all()
            workspace_service.runner.shutdown()

    app = Starlette(
        lifespan=lifespan,
        routes=[Mount(API_PREFIX, routes=routes), *_frontend_routes()],
        middleware=middleware,
        exception_handlers={
            errors.AppError: errors.app_error_handler,
            HTTPException: errors.http_exception_handler,
        },
    )
    app.state.settings = settings
    app.state.db = db
    app.state.auth = auth_service
    app.state.detect_capabilities = detector
    app.state.templates = registry
    app.state.workspaces = workspace_service
    app.state.files = FileService(workspace_service, settings)
    app.state.maintenance = maintenance
    app.state.terminals = terminals
    app.state.streams = streams
    app.state.image_builder = image_builder
    return app


PUBLIC_ROUTES = system.PUBLIC_ROUTES | auth_api.PUBLIC_ROUTES
