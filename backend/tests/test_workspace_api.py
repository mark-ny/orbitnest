"""HTTP-level tests: auth, CSRF, ownership, validation and the full workspace flow."""

from __future__ import annotations

import unittest

from app.main import create_app
from app.runtimes.fake import FakeRuntime
from app.services.jobs import InlineRunner
from tests.asgi_client import AsgiClient
from tests.helpers import PASSWORD, TempDirMixin, fast_hasher, make_caps, make_settings


class ApiBase(TempDirMixin, unittest.TestCase):
    settings_overrides: dict = {}  # noqa: RUF012

    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeRuntime()
        self.app = create_app(
            make_settings(self.tmp, **self.settings_overrides),
            capability_detector=lambda: make_caps({}, tmp=self.tmp),
            password_hasher=fast_hasher(),
            runtimes={"docker": self.fake},
            job_runner=InlineRunner(),
        )
        self.admin = self.setup_admin(AsgiClient(self.app))
        self.alice = self.make_user("alice", "user")
        self.bob = self.make_user("bob", "user")
        self.viewer = self.make_user("viewer", "read_only")

    def make_user(self, name: str, role: str):
        r = self.admin.post("/api/v1/users", json={"username": name, "password": PASSWORD, "role": role})
        self.assertEqual(r.status, 201, r.text)
        return self.login(self.app, name)

    def create(self, who=None, **kw):
        body = {"name": "dev", "template_id": "dev-python", **kw}
        return (who or self.alice).post("/api/v1/workspaces", json=body)


class WorkspaceApiTests(ApiBase):
    def test_full_flow(self) -> None:
        r = self.create()
        self.assertEqual(r.status, 202, r.text)
        ws = r.json()
        self.assertEqual(ws["status"], "provisioning")  # 202 reports the state at acceptance
        self.assertEqual((ws["runtime"], ws["isolation"]), ("docker", "container"))
        wid = ws["id"]
        self.assertEqual(self.alice.get(f"/api/v1/workspaces/{wid}").json()["status"], "running")
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{wid}/stop").status, 202)
        self.assertEqual(self.alice.get(f"/api/v1/workspaces/{wid}").json()["status"], "stopped")
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{wid}/start").status, 202)
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{wid}/restart").status, 202)
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{wid}/activity").status, 200)
        ev = self.alice.get(f"/api/v1/workspaces/{wid}/events").json()["items"]
        self.assertIn("created", [e["event"] for e in ev])
        r = self.alice.patch(f"/api/v1/workspaces/{wid}", json={"name": "renamed", "labels": {"a": "b"}})
        self.assertEqual(r.json()["name"], "renamed")
        r = self.alice.delete(f"/api/v1/workspaces/{wid}")
        self.assertEqual(r.status, 202)
        self.assertEqual(self.alice.get(f"/api/v1/workspaces/{wid}").json()["status"], "deleted")
        self.assertEqual(self.alice.get(f"/api/v1/workspaces/{wid}").json()["data"], "retained")

    def test_delete_data_requires_confirmation_query(self) -> None:
        wid = self.create().json()["id"]
        r = self.alice.delete(f"/api/v1/workspaces/{wid}?delete_data=true")
        self.assertEqual((r.status, r.json()["error"]["code"]), (422, "confirmation_required"))
        self.assertEqual(self.alice.delete(f"/api/v1/workspaces/{wid}?delete_data=maybe").status, 422)
        r = self.alice.delete(f"/api/v1/workspaces/{wid}?delete_data=true&confirm_name=dev")
        self.assertEqual(r.status, 202)
        self.assertEqual(self.alice.get(f"/api/v1/workspaces/{wid}").json()["data"], "purged")

    def test_reset_body_optional_and_validated(self) -> None:
        wid = self.create().json()["id"]
        self.alice.post(f"/api/v1/workspaces/{wid}/stop")
        self.assertEqual(
            self.alice.post(f"/api/v1/workspaces/{wid}/reset", json={"wipe_data": True}).status, 422
        )
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{wid}/reset", json={"bogus": 1}).status, 422)
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{wid}/reset").status, 202)

    def test_csrf_and_cross_origin_rejected(self) -> None:
        body = {"name": "x", "template_id": "dev-python"}
        no_csrf = self.alice.client.post("/api/v1/workspaces", json=body)
        self.assertEqual((no_csrf.status, no_csrf.json()["error"]["code"]), (403, "csrf_failed"))
        evil = self.alice.client.post(
            "/api/v1/workspaces",
            json=body,
            headers={"x-csrf-token": self.alice.csrf, "origin": "https://evil.example"},
        )
        self.assertEqual(evil.status, 403)
        self.assertEqual(self.alice.get("/api/v1/workspaces").json()["total"], 0)

    def test_ownership_isolation_over_http(self) -> None:
        wid = self.create().json()["id"]
        for method, path in [
            ("get", f"/api/v1/workspaces/{wid}"),
            ("post", f"/api/v1/workspaces/{wid}/stop"),
            ("post", f"/api/v1/workspaces/{wid}/restart"),
            ("post", f"/api/v1/workspaces/{wid}/reset"),
            ("post", f"/api/v1/workspaces/{wid}/activity"),
            ("get", f"/api/v1/workspaces/{wid}/events"),
            ("delete", f"/api/v1/workspaces/{wid}"),
        ]:
            r = getattr(self.bob, method)(path)
            self.assertEqual((r.status, r.json()["error"]["code"]), (404, "not_found"), path)
        self.assertEqual(self.bob.patch(f"/api/v1/workspaces/{wid}", json={"name": "z"}).status, 404)
        self.assertEqual(self.bob.get("/api/v1/workspaces").json()["total"], 0)
        self.assertEqual(self.bob.get("/api/v1/workspaces?all=true").status, 403)
        self.assertEqual(
            self.admin.get("/api/v1/workspaces?all=true").json()["items"][0]["owner"]["username"], "alice"
        )
        self.assertEqual(self.admin.post(f"/api/v1/workspaces/{wid}/stop").status, 202)
        self.assertEqual(self.alice.get(f"/api/v1/workspaces/{wid}").json()["status"], "stopped")

    def test_read_only_user(self) -> None:
        self.assertEqual(self.viewer.get("/api/v1/workspaces").status, 200)
        self.assertEqual(self.viewer.get("/api/v1/templates").status, 200)
        self.assertEqual(self.create(self.viewer).status, 403)

    def test_validation_errors_are_422_and_strict(self) -> None:
        for extra in (
            {"name": ""},
            {"name": "a/b"},
            {"profile": "huge"},
            {"unknown_field": 1},
            {"labels": {"A B": "c"}},
            {"idle_timeout_minutes": -5},
        ):
            self.assertEqual(self.create(**extra).status, 422, extra)
        r = self.alice.client.post(
            "/api/v1/workspaces",
            content=b"name=x",
            headers={"x-csrf-token": self.alice.csrf, "content-type": "text/plain"},
        )
        self.assertEqual(r.status, 415)
        self.assertEqual(self.alice.patch("/api/v1/workspaces/abc", json={}).status, 422)

    def test_idempotency_key_header(self) -> None:
        h = {"idempotency-key": "abcdefgh-1234"}
        a = self.alice.post("/api/v1/workspaces", json={"name": "k", "template_id": "dev-python"}, headers=h)
        b = self.alice.post("/api/v1/workspaces", json={"name": "k", "template_id": "dev-python"}, headers=h)
        self.assertEqual((a.status, b.status, a.json()["id"] == b.json()["id"]), (202, 200, True))

    def test_quota_conflict_codes(self) -> None:
        self.admin.patch(
            "/api/v1/settings/policy", json={"max_workspaces_per_user": 1, "max_running_per_user": 1}
        )
        self.assertEqual(self.create(name="one").status, 202)
        r = self.create(name="two")
        self.assertEqual((r.status, r.json()["error"]["code"]), (409, "quota_exceeded"))

    def test_responses_do_not_leak_internals(self) -> None:
        r = self.create(name="leakcheck")
        text = r.text + self.alice.get("/api/v1/workspaces").text
        for needle in (str(self.tmp), "idempotency", "host_path", "resource_id", "fake-0001"):
            self.assertNotIn(needle, text)

    def test_runtime_failure_surfaces_safe_reason(self) -> None:
        self.fake.fail_next("create")
        ws = self.create().json()
        got = self.alice.get(f"/api/v1/workspaces/{ws['id']}").json()
        self.assertEqual(got["status"], "failed")
        self.assertIn("injected", got["status_reason"])


class TemplateAndPolicyApiTests(ApiBase):
    def test_template_list_reports_availability(self) -> None:
        items = {t["id"]: t for t in self.alice.get("/api/v1/templates").json()["items"]}
        self.assertEqual(len(items), 10)
        self.assertTrue(items["dev-python"]["availability"]["creatable"])
        self.assertTrue(items["browser-tor"]["availability"]["creatable"])
        lab = items["security-lab"]
        self.assertEqual(lab["optional_capabilities"], ["NET_RAW", "NET_ADMIN", "SYS_PTRACE"])
        self.assertTrue(lab["rules"])
        self.assertEqual(items["browser-tor"]["privacy"], "tor")
        self.assertEqual(items["browser-chromium"]["privacy"], "direct")
        self.assertNotIn("image", items["dev-python"])  # image/command internals are not exposed
        self.assertEqual(self.alice.get("/api/v1/templates/nope").status, 404)
        self.assertEqual(self.alice.get("/api/v1/templates/dev-node").json()["id"], "dev-node")

    def test_custom_templates_admin_only_and_validated(self) -> None:
        good = {
            "id": "custom-demo",
            "name": "Demo",
            "description": "d",
            "category": "development",
            "image": "alpine:3.20",
            "command": ["sleep", "infinity"],
            "access": "terminal",
        }
        self.assertEqual(self.alice.post("/api/v1/templates", json=good).status, 403)
        r = self.admin.post("/api/v1/templates", json=good)
        self.assertEqual(r.status, 201, r.text)
        self.assertEqual(self.create(template_id="custom-demo", name="c").status, 202)
        bad = {**good, "id": "custom-bad", "image": "--privileged"}
        self.assertEqual(self.admin.post("/api/v1/templates", json=bad).status, 422)
        self.assertEqual(self.admin.delete("/api/v1/templates/custom-demo").status, 409)  # in use
        self.assertEqual(self.admin.patch("/api/v1/templates/dev-node", json={"enabled": False}).status, 403)
        self.assertEqual(self.admin.delete("/api/v1/templates/dev-node").status, 403)
        r = self.admin.patch("/api/v1/templates/custom-demo", json={"enabled": False})
        self.assertFalse(r.json()["enabled"])
        self.assertEqual(self.create(template_id="custom-demo", name="d2").status, 409)

    def test_policy_admin_only_and_validated(self) -> None:
        self.assertEqual(self.alice.get("/api/v1/settings/policy").status, 403)
        self.assertEqual(
            self.alice.patch("/api/v1/settings/policy", json={"max_running_per_user": 9}).status, 403
        )
        self.assertEqual(self.admin.get("/api/v1/settings/policy").json()["max_running_per_user"], 2)
        r = self.admin.patch(
            "/api/v1/settings/policy", json={"max_running_per_user": 4, "idle_action": "mark"}
        )
        self.assertEqual((r.json()["max_running_per_user"], r.json()["idle_action"]), (4, "mark"))
        for bad in ({"max_running_per_user": 0}, {"idle_action": "nuke"}, {}, {"nope": 1}):
            self.assertEqual(self.admin.patch("/api/v1/settings/policy", json=bad).status, 422, bad)
        self.assertEqual(self.admin.get("/api/v1/settings/policy").json()["max_running_per_user"], 4)


class HelperImageBuildApiTests(ApiBase):
    def setUp(self) -> None:
        super().setUp()
        from app.services.image_builder import ImageBuilder

        self.app.state.image_builder = ImageBuilder(planner=lambda ctx: [["true"]])

    def test_only_known_helper_images_are_buildable(self) -> None:
        self.assertEqual(self.admin.post("/api/v1/images/tor-gateway/build").status, 202)
        self.assertEqual(self.admin.post("/api/v1/images/not-a-real-image/build").status, 404)
        self.assertEqual(self.alice.post("/api/v1/images/tor-gateway/build").status, 403)

    def test_status_and_cancel_round_trip(self) -> None:
        self.assertEqual(self.admin.get("/api/v1/images/tor-gateway/build").json()["state"], "idle")
        self.admin.post("/api/v1/images/tor-gateway/build")
        for _ in range(50):
            if self.admin.get("/api/v1/images/tor-gateway/build").json()["state"] != "running":
                break
        r = self.admin.delete("/api/v1/images/tor-gateway/build")
        self.assertIn(r.status, (204, 409))  # 409 if the (instant) fake build already finished


if __name__ == "__main__":
    unittest.main()
