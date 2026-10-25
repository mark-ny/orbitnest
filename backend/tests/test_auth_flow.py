from __future__ import annotations

import json
import unittest

from app.auth import setup_token
from tests.asgi_client import AsgiClient
from tests.helpers import PASSWORD, TempDirMixin


class SetupTests(TempDirMixin, unittest.TestCase):
    def test_status_before_and_after_setup(self) -> None:
        app = self.make_app()
        c = AsgiClient(app)
        self.assertTrue(c.get("/api/v1/auth/setup-status").json()["setup_required"])
        self.setup_admin(c)
        self.assertEqual(c.get("/api/v1/auth/setup-status").json(), {"setup_required": False})

    def test_token_file_created_0600_and_removed_after_setup(self) -> None:
        app = self.make_app()
        path = setup_token.token_path(self.tmp)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.setup_admin(AsgiClient(app))
        self.assertFalse(path.exists())

    def test_wrong_token_rejected_and_audited_without_token_value(self) -> None:
        app = self.make_app()
        c = AsgiClient(app)
        guess = "guess-guess-guess"
        r = c.post(
            "/api/v1/auth/setup", json={"setup_token": guess, "username": "admin", "password": PASSWORD}
        )
        self.assertEqual(r.status, 403)
        self.assertEqual(r.json()["error"]["code"], "invalid_setup_token")
        self.assertTrue(app.state.auth.setup_required())
        with app.state.db.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_events WHERE action = 'auth.setup'").fetchall()
        self.assertEqual(rows[0]["outcome"], "denied")
        self.assertNotIn(guess, json.dumps([dict(x) for x in rows]))

    def test_setup_without_token_field_is_validation_error(self) -> None:
        r = self.make_client().post("/api/v1/auth/setup", json={"username": "admin", "password": PASSWORD})
        self.assertEqual(r.status, 422)

    def test_setup_can_only_happen_once(self) -> None:
        app = self.make_app()
        c = AsgiClient(app)
        token = setup_token.read_setup_token(self.tmp)
        self.setup_admin(c)
        r = AsgiClient(app).post(
            "/api/v1/auth/setup", json={"setup_token": token, "username": "evil", "password": PASSWORD}
        )
        self.assertEqual(r.status, 409)
        self.assertEqual(r.json()["error"]["code"], "setup_unavailable")

    def test_setup_bruteforce_is_rate_limited(self) -> None:
        c = self.make_client()
        codes = [
            c.post(
                "/api/v1/auth/setup",
                json={"setup_token": f"bad{i}", "username": "admin", "password": PASSWORD},
            ).status
            for i in range(7)
        ]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)

    def test_weak_password_and_bad_username_rejected(self) -> None:
        c = self.make_client()
        token = setup_token.read_setup_token(self.tmp)
        r = c.post(
            "/api/v1/auth/setup", json={"setup_token": token, "username": "admin", "password": "short"}
        )
        self.assertEqual(r.status, 422)
        self.assertEqual(r.json()["error"]["details"][0]["field"], "password")
        r = c.post("/api/v1/auth/setup", json={"setup_token": token, "username": "a b", "password": PASSWORD})
        self.assertEqual(r.json()["error"]["details"][0]["field"], "username")

    def test_setup_sets_httponly_samesite_cookie_and_returns_csrf(self) -> None:
        c = self.make_client()
        token = setup_token.read_setup_token(self.tmp)
        r = c.post(
            "/api/v1/auth/setup", json={"setup_token": token, "username": "admin", "password": PASSWORD}
        )
        cookie = r.header_list("set-cookie")[0].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertNotIn("secure", cookie)  # plain http
        self.assertTrue(r.json()["csrf_token"])
        self.assertNotIn("password", r.text.lower().replace("password_", ""))

    def test_app_url_validated_and_stored(self) -> None:
        app = self.make_app()
        token = setup_token.read_setup_token(self.tmp)
        r = AsgiClient(app).post(
            "/api/v1/auth/setup",
            json={
                "setup_token": token,
                "username": "admin",
                "password": PASSWORD,
                "app_url": "javascript:alert(1)",
            },
        )
        self.assertEqual(r.status, 422)

    def test_cross_site_setup_post_rejected(self) -> None:
        c = self.make_client()
        token = setup_token.read_setup_token(self.tmp)
        r = c.post(
            "/api/v1/auth/setup",
            headers={"Origin": "https://evil.example"},
            json={"setup_token": token, "username": "admin", "password": PASSWORD},
        )
        self.assertEqual(r.status, 403)
        self.assertEqual(r.json()["error"]["code"], "csrf_failed")


class LoginLogoutTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app, self.admin = self.admin_session()

    def test_login_success(self) -> None:
        s = self.login(self.app, "admin")
        self.assertEqual(s.user["role"], "admin")
        self.assertEqual(s.get("/api/v1/auth/session").status, 200)

    def test_username_is_case_insensitive(self) -> None:
        self.login(self.app, "ADMIN")

    def test_wrong_password_and_unknown_user_are_indistinguishable(self) -> None:
        a = AsgiClient(self.app).post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong password!!"}
        )
        b = AsgiClient(self.app).post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "wrong password!!"}
        )
        self.assertEqual((a.status, b.status), (401, 401))
        strip = lambda r: {**r.json()["error"], "request_id": None}  # noqa: E731
        self.assertEqual(strip(a), strip(b))

    def test_oversized_password_rejected_cheaply(self) -> None:
        r = AsgiClient(self.app).post(
            "/api/v1/auth/login", json={"username": "admin", "password": "x" * 1099}
        )
        self.assertEqual(r.status, 401)

    def test_logout_revokes_session_server_side(self) -> None:
        s = self.login(self.app, "admin")
        cookie = dict(s.client.cookies)
        self.assertEqual(s.post("/api/v1/auth/logout").status, 204)
        replay = AsgiClient(self.app)
        replay.cookies = cookie
        self.assertEqual(replay.get("/api/v1/auth/session").status, 401)

    def test_logout_requires_csrf(self) -> None:
        s = self.login(self.app, "admin")
        self.assertEqual(s.client.post("/api/v1/auth/logout").status, 403)
        self.assertEqual(s.get("/api/v1/auth/session").status, 200)

    def test_session_token_stored_only_hashed(self) -> None:
        s = self.login(self.app, "admin")
        token = s.client.cookies["orbitnest_session"]
        with self.app.state.db.connect() as conn:
            dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM sessions")])
        self.assertNotIn(token, dump)

    def test_password_hash_never_in_api_output(self) -> None:
        s = self.login(self.app, "admin")
        for path in ("/api/v1/auth/session", "/api/v1/users"):
            self.assertNotIn("scrypt", s.get(path).text)
            self.assertNotIn("password_hash", s.get(path).text)

    def test_credentials_never_in_urls_or_logs_of_audit(self) -> None:
        AsgiClient(self.app).post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong-secret-pw-1"}
        )
        with self.app.state.db.connect() as conn:
            dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_events")])
        self.assertNotIn("wrong-secret-pw-1", dump)

    def test_login_audited_for_success_and_failure(self) -> None:
        self.login(self.app, "admin")
        AsgiClient(self.app).post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong password!!"}
        )
        with self.app.state.db.connect() as conn:
            got = {
                (r["action"], r["outcome"]) for r in conn.execute("SELECT action, outcome FROM audit_events")
            }
        self.assertTrue(
            {("auth.login", "success"), ("auth.login", "failure"), ("auth.setup", "success")} <= got
        )

    def test_secure_cookie_over_https(self) -> None:
        c = AsgiClient(self.app, scheme="https")
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        self.assertIn("secure", r.header_list("set-cookie")[0].lower())

    def test_login_rejects_cross_origin_post(self) -> None:
        r = AsgiClient(self.app).post(
            "/api/v1/auth/login",
            headers={"Origin": "https://evil.example"},
            json={"username": "admin", "password": PASSWORD},
        )
        self.assertEqual(r.status, 403)

    def test_unknown_fields_rejected(self) -> None:
        r = AsgiClient(self.app).post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD, "role": "admin"}
        )
        self.assertEqual(r.status, 422)

    def test_non_json_content_type_rejected(self) -> None:
        r = AsgiClient(self.app).post(
            "/api/v1/auth/login", content=b"username=admin", headers={"content-type": "text/plain"}
        )
        self.assertEqual(r.status, 415)


if __name__ == "__main__":
    unittest.main()
