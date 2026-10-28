from __future__ import annotations

import sys
import time
import unittest

from app.core import errors
from app.services.image_builder import MAX_LOG_LINES, ImageBuilder
from tests.asgi_client import AsgiClient
from tests.test_workspace_api import ApiBase

PY = sys.executable


def planner_for(*scripts: str):
    return lambda _ctx: [[PY, "-c", s] for s in scripts]


def wait_done(builder: ImageBuilder, ctx: str, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = builder.status(ctx)
        if st and st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("build did not finish")


class BuilderTests(unittest.TestCase):
    def test_success_runs_all_steps_and_notifies(self) -> None:
        done = []
        b = ImageBuilder(
            planner=planner_for("print('one')", "print('two')"), on_finished=lambda: done.append(1)
        )
        b.start("stream-chromium")
        st = wait_done(b, "stream-chromium")
        self.assertEqual((st["state"], st["step"], st["steps"]), ("done", 2, 2))
        self.assertIn("one", st["log"])
        self.assertIn("two", st["log"])
        self.assertEqual(done, [1])

    def test_failure_stops_at_failing_step(self) -> None:
        b = ImageBuilder(planner=planner_for("import sys; print('boom'); sys.exit(3)", "print('never')"))
        b.start("stream-chromium")
        st = wait_done(b, "stream-chromium")
        self.assertEqual(st["state"], "failed")
        self.assertIn("exit code 3", st["error"])
        self.assertNotIn("never", st["log"])

    def test_only_one_build_at_a_time_and_unknown_contexts_rejected(self) -> None:
        b = ImageBuilder(planner=planner_for("import time; time.sleep(30)"))
        b.start("stream-chromium")
        try:
            with self.assertRaises(errors.AppError) as cm:
                b.start("stream-firefox")
            self.assertEqual(cm.exception.status_code, 409)
            for bad in ("../etc", "stream-base", "nope", "stream-tor; id"):
                with self.assertRaises(errors.AppError) as cm2:
                    b.start(bad)
                self.assertEqual(cm2.exception.status_code, 404)
        finally:
            self.assertTrue(b.cancel("stream-chromium"))
        st = wait_done(b, "stream-chromium")
        self.assertEqual(st["state"], "cancelled")
        self.assertFalse(b.cancel("stream-chromium"))

    def test_log_is_bounded(self) -> None:
        b = ImageBuilder(planner=planner_for(f"[print('x' * 1000) for _ in range({MAX_LOG_LINES * 3})]"))
        b.start("stream-chromium")
        st = wait_done(b, "stream-chromium")
        self.assertLessEqual(len(st["log"]), MAX_LOG_LINES)
        self.assertTrue(all(len(x) <= 300 for x in st["log"]))

    def test_missing_docker_is_reported_not_raised(self) -> None:
        b = ImageBuilder(planner=lambda _c: [["/nonexistent/docker", "build"]])
        b.start("stream-chromium")
        st = wait_done(b, "stream-chromium")
        self.assertEqual(st["state"], "failed")
        self.assertIn("Docker", st["error"])


class BuildApiTests(ApiBase):
    def setUp(self) -> None:
        super().setUp()
        self.app.state.image_builder = ImageBuilder(planner=planner_for("print('hi')"))
        self.admin_api = self.admin

    def test_only_admins_with_csrf_can_build(self) -> None:
        url = "/api/v1/templates/browser-tor/build"
        self.assertEqual(self.alice.post(url).status, 403)  # regular user
        self.assertEqual(self.alice.get(url).status, 403)
        self.assertEqual(self.alice.delete(url).status, 403)
        self.assertEqual(AsgiClient(self.app).post(url).status, 401)

    def test_unbuildable_template_is_404(self) -> None:
        for tid in ("dev-python", "nope"):
            self.assertEqual(self.admin_api.post(f"/api/v1/templates/{tid}/build").status, 404, tid)

    def test_admin_flow_and_audit(self) -> None:
        r = self.admin_api.post("/api/v1/templates/browser-tor/build")
        self.assertEqual(r.status, 202, r.text)
        st = wait_done(self.app.state.image_builder, "stream-tor")
        self.assertEqual(st["state"], "done")
        got = self.admin_api.get("/api/v1/templates/browser-tor/build").json()
        self.assertEqual(got["state"], "done")
        tpl = {t["id"]: t for t in self.admin_api.get("/api/v1/templates").json()["items"]}
        self.assertTrue(tpl["browser-tor"]["buildable"])
        self.assertFalse(tpl["dev-python"]["buildable"])
        idle = self.admin_api.get("/api/v1/templates/browser-chromium/build").json()
        self.assertEqual(idle["state"], "idle")


if __name__ == "__main__":
    unittest.main()
