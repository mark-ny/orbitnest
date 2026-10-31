"""The administrator System report: problems are plain sentences with a fix, and only admins see it."""

from __future__ import annotations

import os
import time
import unittest

from app.services import sysinfo
from tests.test_workspace_api import ApiBase


class SystemReportTests(ApiBase):
    def setUp(self) -> None:
        super().setUp()
        sysinfo._update_cache.update(
            at=time.time(), value={"checked": True, "behind": 0, "current": "abc1234"}
        )

    def test_admin_only(self) -> None:
        self.assertEqual(self.alice.get("/api/v1/system/report").status, 403)
        self.assertEqual(self.admin.get("/api/v1/system/report").status, 200)

    def test_missing_gateway_and_backup_are_explained_with_a_fix(self) -> None:
        self.fake.images = set()
        body = self.admin.get("/api/v1/system/report").json()
        fixes = {p["what"]: p["fix"] for p in body["problems"]}
        self.assertTrue(any("No backup" in w and "orbitnest backup" in f for w, f in fixes.items()))
        self.assertTrue(any("Tor gateway image" in w for w in fixes))
        self.assertTrue(body["ok"])  # warnings are not errors

    def test_an_installed_gateway_clears_that_warning(self) -> None:
        from app.runtimes.docker_runtime import TOR_GATEWAY_IMAGE

        self.fake.images = {TOR_GATEWAY_IMAGE}
        body = self.admin.get("/api/v1/system/report").json()
        self.assertFalse(any("Tor gateway image" in p["what"] for p in body["problems"]))
        self.assertTrue(next(i for i in body["images"] if i["name"] == "tor-gateway")["present"])

    def test_leftover_gateways_are_counted(self) -> None:
        self.fake.gateways["gw"] = {"orbitnest.workspace": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
        body = self.admin.get("/api/v1/system/report").json()
        self.assertEqual(body["gateways"], {"running": 1, "orphaned": 1})
        self.assertTrue(any("cleanup --apply" in p["fix"] for p in body["problems"]))

    def test_update_endpoint_is_admin_only_and_cached(self) -> None:
        self.assertEqual(self.alice.get("/api/v1/system/update").status, 403)
        self.assertEqual(self.admin.get("/api/v1/system/update").json()["current"], "abc1234")

    def test_available_updates_are_reported(self) -> None:
        sysinfo._update_cache.update(
            at=time.time(), value={"checked": True, "behind": 3, "current": "abc1234"}
        )
        body = self.admin.get("/api/v1/system/report").json()
        self.assertTrue(any("3 update" in p["what"] for p in body["problems"]))


class BackupAgeTests(unittest.TestCase):
    def test_age_is_measured_from_the_newest_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(sysinfo.backup_status(root)["last_backup_days"])
            (root / "backups").mkdir()
            f = root / "backups" / "b.tar.gz"
            f.write_bytes(b"x")
            os.utime(f, (time.time() - 3 * 86400,) * 2)
            self.assertAlmostEqual(sysinfo.backup_status(root)["last_backup_days"], 3.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
