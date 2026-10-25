from __future__ import annotations

import contextlib
import io
import logging
import os
import threading
import unittest
from unittest import mock

from app.__main__ import main
from app.auth import setup_token
from app.main import create_app
from tests.asgi_client import AsgiClient
from tests.helpers import PASSWORD, TempDirMixin, fast_hasher, make_settings


class ConcurrencyTests(TempDirMixin, unittest.TestCase):
    def test_concurrent_setup_creates_exactly_one_admin(self) -> None:
        app = self.make_app()
        service = app.state.auth
        token = setup_token.read_setup_token(self.tmp)
        results: list[str] = []
        barrier = threading.Barrier(6)

        def attempt(i: int) -> None:
            from app.services.audit import RequestContext

            barrier.wait()
            try:
                service.setup_admin(
                    username=f"admin{i}",
                    password=PASSWORD,
                    supplied_token=token,
                    app_url=None,
                    ctx=RequestContext(ip="1.1.1.1"),
                )
                results.append("ok")
            except Exception as exc:  # AppError variants
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(service.user_count(), 1)


class LogHygieneTests(TempDirMixin, unittest.TestCase):
    def test_passwords_and_tokens_never_reach_application_logs(self) -> None:
        stream = io.StringIO()
        app = create_app(make_settings(self.tmp, log_level="DEBUG"), password_hasher=fast_hasher())
        handler = logging.StreamHandler(stream)
        for h in logging.getLogger("orbitnest").handlers:
            handler.setFormatter(h.formatter)
            for f in h.filters:
                handler.addFilter(f)
        logging.getLogger("orbitnest").addHandler(handler)
        self.addCleanup(logging.getLogger("orbitnest").removeHandler, handler)
        token = setup_token.read_setup_token(self.tmp)
        c = AsgiClient(app)
        c.post(
            "/api/v1/auth/setup",
            json={"setup_token": token, "username": "admin", "password": "Sup3r-secret-pw-xyz"},
        )
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-secret-pw-abc"})
        out = stream.getvalue()
        for secret in (
            "Sup3r-secret-pw-xyz",
            "wrong-secret-pw-abc",
            token,
            c.cookies.get("orbitnest_session", "no-cookie"),
        ):
            self.assertNotIn(secret, out)


class RecoveryCliTests(TempDirMixin, unittest.TestCase):
    def run_cli(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
        env = {"ORBITNEST_ENV": "test", "ORBITNEST_DATA_DIR": str(self.tmp), "HOME": str(self.tmp)}
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("sys.stdin", io.StringIO(stdin)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_setup_token_command_then_create_admin_then_login(self) -> None:
        code, out, _ = self.run_cli(["setup-token"])
        self.assertEqual(code, 0)
        self.assertIn("setup token:", out)
        code, out, _ = self.run_cli(["create-admin", "rescue", "--password-stdin"], PASSWORD + "\n")
        self.assertEqual(code, 0, out)
        self.assertFalse(setup_token.token_path(self.tmp).exists())
        app = self.make_app()
        self.assertEqual(self.login(app, "rescue").user["role"], "admin")
        self.assertIn("already complete", self.run_cli(["setup-token"])[1])

    def test_create_admin_rejects_weak_password_and_duplicates(self) -> None:
        code, _, err = self.run_cli(["create-admin", "rescue", "--password-stdin"], "short\n")
        self.assertEqual(code, 1)
        self.assertIn("at least 12", err)
        self.run_cli(["create-admin", "rescue", "--password-stdin"], PASSWORD + "\n")
        code, _, err = self.run_cli(["create-admin", "rescue", "--password-stdin"], PASSWORD + "\n")
        self.assertEqual(code, 1)
        self.assertIn("already taken", err)

    def test_lost_admin_password_recovery_revokes_sessions_and_unlocks(self) -> None:
        app, admin = self.admin_session()
        for _ in range(5):
            AsgiClient(app).post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong password!!"}
            )
        self.assertEqual(
            AsgiClient(app)
            .post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
            .status,
            429,
        )
        new_pw = "brand new recovery pass"
        code, out, _ = self.run_cli(["reset-password", "admin", "--password-stdin"], new_pw + "\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(admin.get("/api/v1/auth/session").status, 401)
        self.login(app, "admin", new_pw)

    def test_reset_unknown_user_fails(self) -> None:
        self.make_app()
        code, _, err = self.run_cli(["reset-password", "ghost", "--password-stdin"], PASSWORD + "\n")
        self.assertEqual(code, 1)
        self.assertIn("not found", err.lower())

    def test_revoke_all_sessions(self) -> None:
        _, admin = self.admin_session()
        code, out, _ = self.run_cli(["revoke-sessions"])
        self.assertEqual(code, 0)
        self.assertIn("revoked 1 session", out)
        self.assertEqual(admin.get("/api/v1/auth/session").status, 401)

    def test_requires_username_argument(self) -> None:
        code, _, _ = self.run_cli(["create-admin"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
