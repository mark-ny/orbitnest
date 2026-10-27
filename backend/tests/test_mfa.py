from __future__ import annotations

import time
import unittest

from app.auth import totp
from tests.asgi_client import AsgiClient
from tests.helpers import PASSWORD, TempDirMixin


class MfaTests(TempDirMixin, unittest.TestCase):
    def enrol(self):
        app, s = self.admin_session()
        r = s.post("/api/v1/auth/mfa/begin", json={"password": PASSWORD})
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        secret = _decode(r.json()["secret"])
        r2 = s.post("/api/v1/auth/mfa/confirm", json={"code": totp.totp(secret, time.time())})
        self.assertEqual(r2.status, 200, r2.text)
        return app, s, secret, r2.json()["recovery_codes"]

    def try_login(self, app, **body):
        return AsgiClient(app).post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD, **body}
        )

    def test_enrol_then_login_requires_code(self) -> None:
        app, s, _secret, codes = self.enrol()
        self.assertEqual(len(codes), 10)
        self.assertEqual(s.get("/api/v1/auth/mfa").json(), {"enabled": True, "recovery_codes_left": 10})
        r = self.try_login(app)
        self.assertEqual((r.status, r.json()["error"]["code"]), (401, "mfa_required"))
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        bad = self.try_login(app, code="000000")
        self.assertEqual((bad.status, bad.json()["error"]["code"]), (401, "invalid_credentials"))

    def test_code_cannot_be_replayed(self) -> None:
        app, _, secret, _ = self.enrol()
        # The confirm step consumed the current step; a later step is still fine, the same one is not.
        code = totp.totp(secret, time.time() + 30)
        self.assertEqual(self.try_login(app, code=code).status, 200)
        self.assertEqual(self.try_login(app, code=code).status, 401)

    def test_recovery_code_is_single_use(self) -> None:
        app, s, _, codes = self.enrol()
        self.assertEqual(self.try_login(app, code=codes[0]).status, 200)
        self.assertEqual(self.try_login(app, code=codes[0]).status, 401)
        self.assertEqual(s.get("/api/v1/auth/mfa").json()["recovery_codes_left"], 9)

    def test_wrong_codes_trigger_lockout(self) -> None:
        app, *_ = self.enrol()
        statuses = [self.try_login(app, code="111111").status for _ in range(7)]
        self.assertEqual(statuses[0], 401)
        self.assertEqual(statuses[-1], 429)

    def test_secret_is_not_stored_in_clear(self) -> None:
        app, _, secret, _ = self.enrol()
        with app.state.db.connect() as conn:
            sealed = conn.execute("SELECT secret_sealed FROM user_totp").fetchone()[0]
            hashes = [r[0] for r in conn.execute("SELECT code_hash FROM recovery_codes")]
        self.assertTrue(sealed.startswith("v1."))
        self.assertNotIn(_b32(secret), sealed)
        self.assertTrue(all(len(h) == 64 for h in hashes))

    def test_confirm_rejects_bad_code_and_begin_needs_password(self) -> None:
        app, s = self.admin_session()
        self.assertEqual(s.post("/api/v1/auth/mfa/begin", json={"password": "wrong-password-1"}).status, 401)
        self.assertEqual(
            s.post("/api/v1/auth/mfa/confirm", json={"code": "123456"}).status, 409
        )  # no enrolment
        s.post("/api/v1/auth/mfa/begin", json={"password": PASSWORD})
        self.assertEqual(s.post("/api/v1/auth/mfa/confirm", json={"code": "000000"}).status, 401)
        self.assertEqual(s.get("/api/v1/auth/mfa").json()["enabled"], False)
        self.assertEqual(self.try_login(app).status, 200)  # unconfirmed enrolment does not gate login

    def test_disable_needs_password_and_code(self) -> None:
        app, s, _secret, codes = self.enrol()
        self.assertEqual(
            s.post("/api/v1/auth/mfa/disable", json={"password": "nope-nope-nope1", "code": codes[0]}).status,
            401,
        )
        self.assertEqual(
            s.post("/api/v1/auth/mfa/disable", json={"password": PASSWORD, "code": "000000"}).status, 401
        )
        self.assertEqual(
            s.post("/api/v1/auth/mfa/disable", json={"password": PASSWORD, "code": codes[1]}).status, 204
        )
        self.assertEqual(self.try_login(app).status, 200)

    def test_admin_reset_and_cli_removal(self) -> None:
        app, s, _, _ = self.enrol()
        uid = s.user["id"]
        self.assertEqual(s.delete(f"/api/v1/users/{uid}/mfa").status, 204)
        self.assertEqual(self.try_login(app).status, 200)
        app.state.auth.disable_mfa_local("admin")  # idempotent, works with nothing enrolled

    def test_non_admin_cannot_reset_mfa(self) -> None:
        app, s = self.admin_session()
        s.post("/api/v1/users", json={"username": "bob", "password": PASSWORD, "role": "user"})
        bob = self.login(app, "bob")
        self.assertEqual(bob.delete(f"/api/v1/users/{s.user['id']}/mfa").status, 403)


def _b32(raw: bytes) -> str:
    return totp.b32(raw)


def _decode(b32: str) -> bytes:
    import base64

    return base64.b32decode(b32 + "=" * (-len(b32) % 8))


if __name__ == "__main__":
    unittest.main()


class MfaCsrfTests(TempDirMixin, unittest.TestCase):
    def test_state_changing_mfa_routes_need_csrf(self) -> None:
        _app, s = self.admin_session()
        for method, path, body in [
            ("post", "/api/v1/auth/mfa/begin", {"password": PASSWORD}),
            ("post", "/api/v1/auth/mfa/confirm", {"code": "123456"}),
            ("post", "/api/v1/auth/mfa/disable", {"password": PASSWORD, "code": "123456"}),
            ("delete", f"/api/v1/users/{s.user['id']}/mfa", None),
        ]:
            kw = {"json": body} if body is not None else {}
            r = getattr(s.client, method)(path, **kw)
            self.assertEqual(r.status, 403, path)
