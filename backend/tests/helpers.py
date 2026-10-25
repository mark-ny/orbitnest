from __future__ import annotations

import tempfile
from pathlib import Path

from app.auth import setup_token
from app.auth.passwords import PasswordHasher
from app.core.config import Settings
from app.main import create_app
from tests.asgi_client import AsgiClient

PASSWORD = "correct horse battery"


def fast_hasher() -> PasswordHasher:
    """Cheap scrypt parameters so the suite stays fast. Production defaults are tested separately."""
    return PasswordHasher(scrypt_log_n=10, scrypt_r=1, scrypt_p=1, prefer_argon2=False)


def make_settings(tmp: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {"env": "test", "data_dir": tmp, "log_level": "CRITICAL"}
    values.update(overrides)
    return Settings.model_validate(values)


class Session:
    """An AsgiClient plus the CSRF token of its logged-in session."""

    def __init__(self, client: AsgiClient, csrf: str, user: dict) -> None:
        self.client, self.csrf, self.user = client, csrf, user

    def _h(self, headers: dict | None) -> dict:
        return {"x-csrf-token": self.csrf, **(headers or {})}

    def get(self, path: str, **kw):
        return self.client.get(path, **kw)

    def post(self, path: str, **kw):
        kw["headers"] = self._h(kw.get("headers"))
        return self.client.post(path, **kw)

    def patch(self, path: str, **kw):
        kw["headers"] = self._h(kw.get("headers"))
        return self.client.patch(path, **kw)

    def delete(self, path: str, **kw):
        kw["headers"] = self._h(kw.get("headers"))
        return self.client.delete(path, **kw)


class TempDirMixin:
    """unittest mixin giving each test an isolated data dir and app factory."""

    tmp: Path

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        super().setUp()  # type: ignore[misc]

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()  # type: ignore[misc]

    def make_app(self, **overrides: object):
        return create_app(make_settings(self.tmp, **overrides), password_hasher=fast_hasher())

    def make_client(self, **overrides: object) -> AsgiClient:
        return AsgiClient(self.make_app(**overrides))

    def setup_admin(self, client: AsgiClient, username: str = "admin", password: str = PASSWORD) -> Session:
        token = setup_token.read_setup_token(self.tmp)
        assert token, "setup token should exist before first-run setup"
        r = client.post(
            "/api/v1/auth/setup",
            json={"setup_token": token, "username": username, "password": password},
        )
        assert r.status == 201, r.text
        body = r.json()
        return Session(client, body["csrf_token"], body["user"])

    def login(self, app, username: str, password: str = PASSWORD, **client_kw) -> Session:
        client = AsgiClient(app, **client_kw)
        r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status == 200, r.text
        body = r.json()
        return Session(client, body["csrf_token"], body["user"])

    def admin_session(self, **overrides: object) -> tuple[object, Session]:
        app = self.make_app(**overrides)
        return app, self.setup_admin(AsgiClient(app))


# -- workspace service harness -----------------------------------------------------------------
import threading  # noqa: E402
import unittest  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from app.core.platform import CommandResult, detect_capabilities  # noqa: E402
from app.db import Database, migrate  # noqa: E402
from app.runtimes.fake import FakeRuntime  # noqa: E402
from app.services.jobs import InlineRunner, ManualRunner  # noqa: E402
from app.services.templates import TemplateRegistry  # noqa: E402
from app.services.workspace_store import WorkspaceStore  # noqa: E402
from app.services.workspaces import WorkspaceService  # noqa: E402
from app.storage.manager import StorageManager  # noqa: E402


def make_caps(env: dict, plat: str = "linux", docker: bool = True, tmp: Path | str = "/tmp"):
    def runner(argv, timeout):
        if argv[1:2] == ["version"]:
            return CommandResult(0, "27.0", "") if docker else CommandResult(1, "", "no daemon")
        return CommandResult(0, "2.0", "")

    return detect_capabilities(
        Path(tmp),
        environ=env,
        sys_platform=plat,
        which=lambda n: f"/usr/bin/{n}" if (docker or n != "docker") else None,
        runner=runner,
    )


@dataclass(frozen=True)
class Actor:
    id: str
    role: str


class FakeTime:
    """Controllable wall clock + monotonic clock; sleeping advances both."""

    def __init__(self) -> None:
        self.wall = datetime.now(UTC)
        self.mono = 0.0

    def now(self) -> datetime:
        return self.wall

    def clock(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)

    def advance(self, minutes: float) -> None:
        self.wall += timedelta(minutes=minutes)


class ServiceHarness(TempDirMixin, unittest.TestCase):
    """A WorkspaceService over a real SQLite database and the FakeRuntime."""

    runner_cls = InlineRunner

    def setUp(self) -> None:
        super().setUp()
        self.settings = make_settings(self.tmp, start_timeout_seconds=5)
        self.db = Database(self.settings.db_path)
        migrate(self.db)
        self.fake = FakeRuntime()
        self.time = FakeTime()
        self.runner = self.runner_cls()
        self.caps = make_caps({}, tmp=self.tmp)
        self.service = self.new_service()
        self.store = self.service.store
        self.admin = self.add_user("admin1", "admin")
        self.alice = self.add_user("alice", "user")
        self.bob = self.add_user("bob", "user")

    def new_service(self, **kw) -> WorkspaceService:
        args = dict(
            db=self.db,
            settings=self.settings,
            store=WorkspaceStore(self.db),
            registry=TemplateRegistry(self.db),
            storage=StorageManager(self.settings.workspaces_root),
            runtimes={"docker": self.fake},
            runner=self.runner,
            capabilities=lambda: self.caps,
            sleep=self.time.sleep,
            clock=self.time.clock,
            now=self.time.now,
        )
        args.update(kw)
        return WorkspaceService(**args)

    def add_user(self, name: str, role: str) -> Actor:
        from app.core.ids import new_id

        uid = new_id()
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, 'x', ?)",
                (uid, name, role),
            )
        return Actor(uid, role)

    def make_ws(self, actor=None, name: str = "dev one", template: str = "terminal-ubuntu", **kw):
        ws, _ = self.service.create(actor or self.alice, name=name, template_id=template, **kw)
        return ws

    def status(self, ws) -> str:
        return self.store.get(ws.id).status

    def concurrently(self, fns):
        barrier = threading.Barrier(len(fns))
        results: list = [None] * len(fns)

        def run(i, fn):
            barrier.wait()
            try:
                results[i] = ("ok", fn())
            except Exception as exc:
                results[i] = ("err", exc)

        threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results


__all__ = ["ManualRunner"]
