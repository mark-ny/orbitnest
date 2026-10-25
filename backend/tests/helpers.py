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
