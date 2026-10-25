from __future__ import annotations

import unittest

from app.auth.policy import Perm, can_access_workspace, has_permission
from tests.asgi_client import AsgiClient
from tests.helpers import PASSWORD, TempDirMixin

NEW_PW = "another good passphrase"


class PolicyUnitTests(unittest.TestCase):
    def test_role_permissions(self) -> None:
        self.assertTrue(has_permission("admin", Perm.USERS_MANAGE))
        self.assertFalse(has_permission("user", Perm.USERS_MANAGE))
        self.assertFalse(has_permission("user", Perm.AUDIT_READ))
        self.assertTrue(has_permission("user", Perm.WORKSPACE_WRITE))
        self.assertTrue(has_permission("read_only", Perm.WORKSPACE_READ))
        self.assertFalse(has_permission("read_only", Perm.WORKSPACE_WRITE))
        self.assertFalse(has_permission("nonsense", Perm.SYSTEM_READ))

    def test_workspace_access_rules(self) -> None:
        f = can_access_workspace
        self.assertTrue(f(user_id="a", role="user", owner_id="a", write=True))
        self.assertFalse(f(user_id="b", role="user", owner_id="a", write=False))
        self.assertTrue(f(user_id="adm", role="admin", owner_id="a", write=True))
        self.assertTrue(f(user_id="a", role="read_only", owner_id="a", write=False))
        self.assertFalse(f(user_id="a", role="read_only", owner_id="a", write=True))


class UserAdminTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app, self.admin = self.admin_session()

    def make(self, name: str, role: str = "user"):
        r = self.admin.post("/api/v1/users", json={"username": name, "password": PASSWORD, "role": role})
        self.assertEqual(r.status, 201, r.text)
        return r.json()

    def test_admin_creates_and_lists(self) -> None:
        self.make("bob")
        names = {u["username"] for u in self.admin.get("/api/v1/users").json()["items"]}
        self.assertEqual(names, {"admin", "bob"})

    def test_duplicate_username_case_insensitive(self) -> None:
        self.make("bob")
        r = self.admin.post("/api/v1/users", json={"username": "BOB", "password": PASSWORD})
        self.assertEqual(r.status, 409)

    def test_non_admins_forbidden_on_user_and_audit_endpoints(self) -> None:
        self.make("bob")
        self.make("reader", "read_only")
        for name in ("bob", "reader"):
            s = self.login(self.app, name)
            self.assertEqual(s.get("/api/v1/users").status, 403)
            self.assertEqual(
                s.post("/api/v1/users", json={"username": "zzz", "password": PASSWORD}).status, 403
            )
            self.assertEqual(s.get("/api/v1/audit-events").status, 403)
            self.assertEqual(s.patch(f"/api/v1/users/{s.user['id']}", json={"role": "admin"}).status, 403)

    def test_privilege_escalation_via_self_patch_blocked_and_audited(self) -> None:
        self.make("bob")
        bob = self.login(self.app, "bob")
        self.assertEqual(bob.patch(f"/api/v1/users/{bob.user['id']}", json={"role": "admin"}).status, 403)
        events = self.admin.get("/api/v1/audit-events", params={"action": "authz.denied"}).json()["items"]
        self.assertTrue(events)

    def test_cannot_demote_or_deactivate_last_admin(self) -> None:
        me = self.admin.user["id"]
        for body in ({"role": "user"}, {"is_active": False}):
            r = self.admin.patch(f"/api/v1/users/{me}", json=body)
            self.assertEqual(r.status, 409)
            self.assertEqual(r.json()["error"]["code"], "last_admin")

    def test_can_demote_when_another_admin_exists(self) -> None:
        other = self.make("root2", "admin")
        self.assertEqual(self.admin.patch(f"/api/v1/users/{other['id']}", json={"role": "user"}).status, 200)

    def test_role_change_revokes_target_sessions(self) -> None:
        b = self.make("bob")
        bob = self.login(self.app, "bob")
        self.admin.patch(f"/api/v1/users/{b['id']}", json={"role": "read_only"})
        self.assertEqual(bob.get("/api/v1/auth/session").status, 401)

    def test_validation(self) -> None:
        self.assertEqual(
            self.admin.post(
                "/api/v1/users", json={"username": "ok_name", "password": PASSWORD, "role": "root"}
            ).status,
            422,
        )
        self.assertEqual(self.admin.patch("/api/v1/users/x", json={}).status, 422)
        self.assertEqual(self.admin.patch("/api/v1/users/doesnotexist", json={"role": "user"}).status, 404)
        self.assertEqual(self.admin.get("/api/v1/users", params={"limit": "abc"}).status, 422)

    def test_unauthenticated_user_endpoints_401(self) -> None:
        c = AsgiClient(self.app)
        self.assertEqual(c.get("/api/v1/users").status, 401)
        self.assertEqual(c.get("/api/v1/audit-events").status, 401)


class PasswordChangeResetTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app, self.admin = self.admin_session()
        self.admin.post("/api/v1/users", json={"username": "bob", "password": PASSWORD, "role": "user"})
        self.bob = self.login(self.app, "bob")
        self.bob_id = self.bob.user["id"]

    def test_change_password_revokes_other_sessions_keeps_current(self) -> None:
        other = self.login(self.app, "bob")
        r = self.bob.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PW}
        )
        self.assertEqual(r.status, 204)
        self.assertEqual(self.bob.get("/api/v1/auth/session").status, 200)
        self.assertEqual(other.get("/api/v1/auth/session").status, 401)
        self.login(self.app, "bob", NEW_PW)
        self.assertEqual(
            AsgiClient(self.app)
            .post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD})
            .status,
            401,
        )

    def test_change_password_wrong_current_and_weak_new(self) -> None:
        r = self.bob.post(
            "/api/v1/auth/password", json={"current_password": "nope nope nope", "new_password": NEW_PW}
        )
        self.assertEqual(r.status, 401)
        r = self.bob.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
        )
        self.assertEqual(r.status, 422)
        r = self.bob.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD}
        )
        self.assertEqual(r.status, 422)

    def test_change_password_guess_is_rate_limited(self) -> None:
        codes = [
            self.bob.post(
                "/api/v1/auth/password", json={"current_password": f"wrong guess {i}", "new_password": NEW_PW}
            ).status
            for i in range(6)
        ]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)

    def test_admin_reset_flow(self) -> None:
        issued = self.admin.post(f"/api/v1/users/{self.bob_id}/password-reset")
        self.assertEqual(issued.status, 201)
        token = issued.json()["token"]
        anon = AsgiClient(self.app)
        self.assertEqual(
            anon.post("/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PW}).status,
            204,
        )
        self.assertEqual(self.bob.get("/api/v1/auth/session").status, 401, "old sessions revoked")
        self.login(self.app, "bob", NEW_PW)

    def test_reset_token_single_use_and_stored_hashed(self) -> None:
        token = self.admin.post(f"/api/v1/users/{self.bob_id}/password-reset").json()["token"]
        with self.app.state.db.connect() as conn:
            self.assertNotIn(token, str([tuple(r) for r in conn.execute("SELECT * FROM password_resets")]))
        anon = AsgiClient(self.app)
        self.assertEqual(
            anon.post("/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PW}).status,
            204,
        )
        r = anon.post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": "third good passphrase"}
        )
        self.assertEqual((r.status, r.json()["error"]["code"]), (400, "invalid_reset_token"))

    def test_reset_token_expires(self) -> None:
        from datetime import timedelta

        token = self.admin.post(f"/api/v1/users/{self.bob_id}/password-reset").json()["token"]
        real = self.app.state.auth.now
        self.app.state.auth.now = lambda: real() + timedelta(minutes=31)
        r = AsgiClient(self.app).post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PW}
        )
        self.assertEqual(r.status, 400)

    def test_new_reset_invalidates_previous_token(self) -> None:
        first = self.admin.post(f"/api/v1/users/{self.bob_id}/password-reset").json()["token"]
        self.admin.post(f"/api/v1/users/{self.bob_id}/password-reset")
        r = AsgiClient(self.app).post(
            "/api/v1/auth/reset-password", json={"token": first, "new_password": NEW_PW}
        )
        self.assertEqual(r.status, 400)

    def test_reset_guessing_rate_limited(self) -> None:
        codes = [
            AsgiClient(self.app)
            .post("/api/v1/auth/reset-password", json={"token": f"bad{i}", "new_password": NEW_PW})
            .status
            for i in range(12)
        ]
        self.assertEqual(codes[0], 400)
        self.assertEqual(codes[-1], 429)

    def test_deactivated_user_cannot_redeem_reset_token(self) -> None:
        token = self.admin.post(f"/api/v1/users/{self.bob_id}/password-reset").json()["token"]
        self.admin.patch(f"/api/v1/users/{self.bob_id}", json={"is_active": False})
        r = AsgiClient(self.app).post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PW}
        )
        self.assertEqual(r.status, 400)

    def test_only_admin_can_issue_reset(self) -> None:
        self.assertEqual(self.bob.post(f"/api/v1/users/{self.bob_id}/password-reset").status, 403)
        self.assertEqual(self.admin.post("/api/v1/users/nope/password-reset").status, 404)


class AuditApiTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app, self.admin = self.admin_session()

    def test_pagination_filters_and_cursor(self) -> None:
        for i in range(6):
            AsgiClient(self.app, client=(f"198.51.100.{i}", 1)).post(
                "/api/v1/auth/login", json={"username": f"ghost{i}", "password": "wrong password!!"}
            )
        page1 = self.admin.get("/api/v1/audit-events", params={"limit": "4", "outcome": "failure"}).json()
        self.assertEqual(len(page1["items"]), 4)
        self.assertTrue(page1["next_cursor"])
        page2 = self.admin.get(
            "/api/v1/audit-events",
            params={"limit": "4", "outcome": "failure", "cursor": page1["next_cursor"]},
        ).json()
        ids = [e["id"] for e in page1["items"] + page2["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(page2["items"]), 2)
        self.assertIsNone(page2["next_cursor"])

    def test_bad_params(self) -> None:
        self.assertEqual(self.admin.get("/api/v1/audit-events", params={"outcome": "bogus"}).status, 422)
        self.assertEqual(self.admin.get("/api/v1/audit-events", params={"cursor": "!!!"}).status, 422)
        self.assertEqual(self.admin.get("/api/v1/audit-events", params={"limit": "x"}).status, 422)

    def test_like_wildcards_are_escaped(self) -> None:
        r = self.admin.get("/api/v1/audit-events", params={"action": "%"}).json()
        self.assertEqual(r["items"], [])

    def test_user_lifecycle_audited(self) -> None:
        u = self.admin.post("/api/v1/users", json={"username": "bob", "password": PASSWORD}).json()
        self.admin.patch(f"/api/v1/users/{u['id']}", json={"is_active": False})
        actions = [
            e["action"]
            for e in self.admin.get("/api/v1/audit-events", params={"action": "user."}).json()["items"]
        ]
        self.assertEqual(sorted(actions), ["user.create", "user.update"])


if __name__ == "__main__":
    unittest.main()
