from __future__ import annotations

import unittest
from datetime import timedelta

from app.core.ids import utcnow
from tests.asgi_client import AsgiClient
from tests.helpers import PASSWORD, TempDirMixin


class Clock:
    def __init__(self) -> None:
        self.t = utcnow()

    def __call__(self):
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class LockoutTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app, _ = self.admin_session()
        self.clock = Clock()
        self.app.state.auth.now = self.clock

    def bad(self, user: str = "admin", ip: str = "198.51.100.1"):
        return AsgiClient(self.app, client=(ip, 1)).post(
            "/api/v1/auth/login", json={"username": user, "password": "wrong password!!"}
        )

    def good(self, user: str = "admin", ip: str = "198.51.100.1"):
        return AsgiClient(self.app, client=(ip, 1)).post(
            "/api/v1/auth/login", json={"username": user, "password": PASSWORD}
        )

    def test_lockout_after_five_failures_blocks_even_correct_password(self) -> None:
        for _ in range(5):
            self.assertEqual(self.bad().status, 401)
        r = self.good()
        self.assertEqual(r.status, 429)
        self.assertEqual(r.json()["error"]["code"], "rate_limited")
        self.assertGreater(int(r.headers["retry-after"]), 0)

    def test_lockout_expires_with_backoff(self) -> None:
        for _ in range(5):
            self.bad()
        self.clock.advance(seconds=31)
        self.assertEqual(self.good().status, 200)

    def test_backoff_grows_with_more_failures(self) -> None:
        for _ in range(5):
            self.bad()
        self.clock.advance(seconds=31)
        self.assertEqual(self.bad().status, 401)  # 6th failure: lock is now 60s
        self.clock.advance(seconds=31)
        self.assertEqual(self.good().status, 429)
        self.clock.advance(seconds=31)
        self.assertEqual(self.good().status, 200)

    def test_success_clears_failure_counter(self) -> None:
        for _ in range(4):
            self.bad()
        self.assertEqual(self.good().status, 200)
        for _ in range(4):
            self.assertEqual(self.bad().status, 401)

    def test_unknown_usernames_are_throttled_identically(self) -> None:
        for _ in range(5):
            self.bad(user="ghost")
        r = self.bad(user="ghost")
        self.assertEqual(r.status, 429)

    def test_per_ip_limit_across_usernames(self) -> None:
        for i in range(20):
            self.bad(user=f"user{i}", ip="203.0.113.7")
        self.assertEqual(self.bad(user="fresh", ip="203.0.113.7").status, 429)
        self.assertEqual(self.good(ip="203.0.113.8").status, 200)

    def test_failures_age_out_of_window(self) -> None:
        for _ in range(4):
            self.bad()
        self.clock.advance(minutes=16)
        for _ in range(4):
            self.assertEqual(self.bad().status, 401)

    def test_xff_ignored_unless_proxy_trusted(self) -> None:
        for i in range(20):
            AsgiClient(self.app, client=("198.51.100.50", 1)).post(
                "/api/v1/auth/login",
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
                json={"username": f"u{i}", "password": "wrong password!!"},
            )
        r = AsgiClient(self.app, client=("198.51.100.50", 1)).post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": "10.9.9.9"},
            json={"username": "other", "password": "wrong password!!"},
        )
        self.assertEqual(r.status, 429, "spoofed X-Forwarded-For must not bypass per-IP limit")


class SessionLifetimeTests(TempDirMixin, unittest.TestCase):
    def test_idle_timeout(self) -> None:
        app, _ = self.admin_session(session_idle_minutes=30)
        clock = Clock()
        app.state.auth.now = clock
        s = self.login(app, "admin")
        clock.advance(minutes=29)
        self.assertEqual(s.get("/api/v1/auth/session").status, 200)
        clock.advance(minutes=31)
        self.assertEqual(s.get("/api/v1/auth/session").status, 401)

    def test_activity_extends_idle_window_but_not_absolute(self) -> None:
        app, _ = self.admin_session(session_idle_minutes=30, session_absolute_hours=1)
        clock = Clock()
        app.state.auth.now = clock
        s = self.login(app, "admin")
        for _ in range(5):
            clock.advance(minutes=11)
            self.assertEqual(s.get("/api/v1/auth/session").status, 200)
        clock.advance(minutes=11)  # >60 minutes since login
        self.assertEqual(s.get("/api/v1/auth/session").status, 401)

    def test_deactivated_user_session_stops_working(self) -> None:
        app, admin = self.admin_session()
        admin.post("/api/v1/users", json={"username": "bob", "password": PASSWORD, "role": "user"})
        bob = self.login(app, "bob")
        uid = bob.user["id"]
        self.assertEqual(admin.patch(f"/api/v1/users/{uid}", json={"is_active": False}).status, 200)
        self.assertEqual(bob.get("/api/v1/auth/session").status, 401)
        r = AsgiClient(app).post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD})
        self.assertEqual(r.status, 401)

    def test_garbage_cookie_rejected(self) -> None:
        c = self.make_client()
        for value in ("", "x", "a" * 500, "' OR 1=1 --"):
            c.cookies = {"orbitnest_session": value}
            self.assertEqual(c.get("/api/v1/auth/session").status, 401)

    def test_list_and_revoke_own_sessions(self) -> None:
        app, _ = self.admin_session()
        a, b = self.login(app, "admin"), self.login(app, "admin")
        items = a.get("/api/v1/auth/sessions").json()["items"]
        self.assertGreaterEqual(len(items), 3)
        other = next(i for i in items if not i["current"])
        self.assertEqual(a.delete(f"/api/v1/auth/sessions/{other['id']}").status, 204)
        self.assertEqual(a.delete(f"/api/v1/auth/sessions/{other['id']}").status, 404)
        self.assertNotIn("token", a.get("/api/v1/auth/sessions").text)
        del b

    def test_cannot_revoke_another_users_session(self) -> None:
        app, admin = self.admin_session()
        admin.post("/api/v1/users", json={"username": "bob", "password": PASSWORD, "role": "user"})
        admin.post("/api/v1/users", json={"username": "eve", "password": PASSWORD, "role": "user"})
        bob, eve = self.login(app, "bob"), self.login(app, "eve")
        bob_sid = bob.get("/api/v1/auth/sessions").json()["items"][0]["id"]
        self.assertEqual(eve.delete(f"/api/v1/auth/sessions/{bob_sid}").status, 404)
        self.assertEqual(bob.get("/api/v1/auth/session").status, 200)


class CsrfTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app, self.admin = self.admin_session()

    def test_missing_or_wrong_token_rejected_on_every_unsafe_method(self) -> None:
        c = self.admin.client
        body = {"username": "x1x", "password": PASSWORD, "role": "user"}
        self.assertEqual(c.post("/api/v1/users", json=body).status, 403)
        self.assertEqual(c.post("/api/v1/users", json=body, headers={"x-csrf-token": "nope"}).status, 403)
        self.assertEqual(c.patch("/api/v1/users/x", json={"is_active": False}).status, 403)
        self.assertEqual(
            c.post("/api/v1/auth/password", json={"current_password": "a", "new_password": "b"}).status, 403
        )
        self.assertEqual(c.delete("/api/v1/auth/sessions/x").status, 403)

    def test_token_from_another_session_rejected(self) -> None:
        other = self.login(self.app, "admin")
        r = self.admin.client.post(
            "/api/v1/users",
            json={"username": "x1x", "password": PASSWORD, "role": "user"},
            headers={"x-csrf-token": other.csrf},
        )
        self.assertEqual(r.status, 403)

    def test_cross_origin_with_valid_token_rejected(self) -> None:
        r = self.admin.post(
            "/api/v1/users",
            json={"username": "x1x", "password": PASSWORD, "role": "user"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(r.status, 403)

    def test_same_origin_with_valid_token_ok(self) -> None:
        r = self.admin.post(
            "/api/v1/users",
            json={"username": "x1x", "password": PASSWORD, "role": "user"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r.status, 201)

    def test_safe_methods_need_no_csrf(self) -> None:
        self.assertEqual(self.admin.client.get("/api/v1/users").status, 200)

    def test_csrf_rejection_is_audited(self) -> None:
        self.admin.client.post("/api/v1/users", json={"username": "x1x", "password": PASSWORD})
        r = self.admin.get("/api/v1/audit-events", params={"action": "authz.csrf"})
        self.assertEqual(r.json()["items"][0]["outcome"], "denied")


if __name__ == "__main__":
    unittest.main()
