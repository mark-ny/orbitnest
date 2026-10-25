"""File manager over HTTP: ownership, CSRF, traversal/symlink attacks, limits, audit."""

from __future__ import annotations

import os
import unittest

from app.main import create_app
from app.runtimes.fake import FakeRuntime
from app.services.jobs import InlineRunner
from tests.helpers import fast_hasher, make_caps, make_settings
from tests.test_workspace_api import ApiBase


class FilesApiBase(ApiBase):
    def setUp(self) -> None:
        super().setUp()
        self.wid = self.create(name="files").json()["id"]
        self.base = f"/api/v1/workspaces/{self.wid}/files"
        self.vol = self.tmp / "workspaces" / self.wid / "data"

    def put(self, who, path, data=b"x", overwrite=False, **kw):
        q = f"?path={path}" + ("&overwrite=true" if overwrite else "")
        return who.client.request(
            "PUT", f"{self.base}/content{q}", content=data, headers={"x-csrf-token": who.csrf}, **kw
        )


class FileFlowTests(FilesApiBase):
    def test_roundtrip(self) -> None:
        self.assertEqual(self.alice.post(f"{self.base}/mkdir", json={"path": "/docs"}).status, 201)
        r = self.put(self.alice, "/docs/a.txt", b"hello world")
        self.assertEqual((r.status, r.json()), (201, {"path": "/docs/a.txt", "size": 11}))
        listing = self.alice.get(f"{self.base}?path=/docs").json()
        self.assertEqual(
            [(e["name"], e["type"], e["size"]) for e in listing["entries"]], [("a.txt", "file", 11)]
        )
        d = self.alice.get(f"{self.base}/content?path=/docs/a.txt")
        self.assertEqual((d.status, d.body), (200, b"hello world"))
        h = d.headers
        self.assertEqual(h["content-type"], "application/octet-stream")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("attachment", h["content-disposition"])
        self.assertEqual(
            self.alice.post(f"{self.base}/move", json={"source": "/docs", "destination": "/papers"}).status,
            200,
        )
        self.assertEqual(self.alice.delete(f"{self.base}?path=/papers").status, 409)  # not empty
        self.assertEqual(self.alice.delete(f"{self.base}?path=/papers&recursive=true").status, 204)
        self.assertEqual(self.alice.get(self.base).json()["entries"], [])

    def test_hostile_download_name_is_neutralised(self) -> None:
        name = 'we"ird\r\nx.txt'.replace("\r\n", "")
        (self.vol / name).write_text("x")
        r = self.alice.get(f"{self.base}/content", params={"path": "/" + name})
        self.assertEqual(r.status, 200)
        self.assertNotIn("\n", r.headers["content-disposition"])
        self.assertEqual(r.headers["content-disposition"].count('"'), 2)

    def test_files_work_while_stopped_and_audited(self) -> None:
        self.alice.post(f"/api/v1/workspaces/{self.wid}/stop")
        self.assertEqual(self.put(self.alice, "/n.txt", b"1").status, 201)
        self.assertEqual(self.alice.delete(f"{self.base}?path=/n.txt").status, 204)
        events = self.admin.get("/api/v1/audit-events?action=file.").json()["items"]
        self.assertEqual(sorted(e["action"] for e in events), ["file.delete", "file.upload"])

    def test_overwrite_semantics(self) -> None:
        self.assertEqual(self.put(self.alice, "/a", b"1").status, 201)
        r = self.put(self.alice, "/a", b"2")
        self.assertEqual((r.status, r.json()["error"]["code"]), (409, "conflict"))
        self.assertEqual(self.put(self.alice, "/a", b"3", overwrite=True).status, 201)
        self.assertEqual((self.vol / "a").read_bytes(), b"3")

    def test_deleted_workspace_files_unavailable(self) -> None:
        self.alice.delete(f"/api/v1/workspaces/{self.wid}")
        r = self.alice.get(self.base)
        self.assertEqual((r.status, r.json()["error"]["code"]), (409, "workspace_deleted"))


class FileSecurityTests(FilesApiBase):
    def test_csrf_required_for_mutations(self) -> None:
        c = self.alice.client
        for method, path, kw in [
            ("PUT", f"{self.base}/content?path=/x", {"content": b"x"}),
            ("POST", f"{self.base}/mkdir", {"json": {"path": "/d"}}),
            ("POST", f"{self.base}/move", {"json": {"source": "/a", "destination": "/b"}}),
            ("DELETE", f"{self.base}?path=/x", {}),
        ]:
            r = c.request(method, path, **kw)
            self.assertEqual((r.status, r.json()["error"]["code"]), (403, "csrf_failed"), path)
        self.assertEqual(os.listdir(self.vol), [])

    def test_other_users_cannot_touch_files(self) -> None:
        self.put(self.alice, "/secret.txt", b"mine")
        for r in (
            self.bob.get(self.base),
            self.bob.get(f"{self.base}/content?path=/secret.txt"),
            self.put(self.bob, "/evil.txt"),
            self.bob.post(f"{self.base}/mkdir", json={"path": "/d"}),
            self.bob.post(f"{self.base}/move", json={"source": "/secret.txt", "destination": "/x"}),
            self.bob.delete(f"{self.base}?path=/secret.txt"),
        ):
            self.assertEqual((r.status, r.json()["error"]["code"]), (404, "not_found"))
        self.assertEqual(os.listdir(self.vol), ["secret.txt"])
        self.assertEqual(self.admin.get(f"{self.base}/content?path=/secret.txt").body, b"mine")

    def test_traversal_attempts(self) -> None:
        (self.tmp / "outside.txt").write_text("OUT")
        for p in (
            "../../outside.txt",
            "..",
            "/../x",
            "a/../../x",
            "%2e%2e/%2e%2e/outside.txt",
            "a\\..\\b",
            "/etc/passwd\x00.txt",
        ):
            r = self.alice.get(f"{self.base}/content", params={"path": p})
            self.assertIn(r.status, (404, 422), p)
            self.assertNotIn(b"OUT", r.body)
        r = self.put(self.alice, "../escape.txt")
        self.assertEqual(r.status, 422)
        self.assertFalse((self.tmp / "workspaces" / self.wid / "escape.txt").exists())

    def test_symlinks_planted_by_the_workload(self) -> None:
        (self.tmp / "host-secret").write_text("HOSTSECRET")
        os.symlink(self.tmp / "host-secret", self.vol / "peek")
        os.symlink(self.tmp, self.vol / "dir")
        r = self.alice.get(f"{self.base}/content?path=/peek")
        self.assertEqual(r.status, 409)
        self.assertNotIn(b"HOSTSECRET", r.body)
        self.assertEqual(self.alice.get(f"{self.base}?path=/dir").status, 409)
        self.assertEqual(self.put(self.alice, "/dir/new").status, 409)
        listing = self.alice.get(self.base).json()["entries"]
        self.assertEqual({e["name"]: e["type"] for e in listing}, {"peek": "symlink", "dir": "symlink"})
        self.assertEqual(self.alice.delete(f"{self.base}?path=/dir&recursive=true").status, 204)
        self.assertTrue((self.tmp / "host-secret").exists())

    def test_cannot_delete_root_or_leak_paths(self) -> None:
        r = self.alice.delete(f"{self.base}?path=/&recursive=true")
        self.assertEqual(r.status, 403)
        for r in (
            self.alice.get(f"{self.base}?path=/nope"),
            self.alice.get(f"{self.base}/content?path=/nope"),
        ):
            self.assertEqual(r.status, 404)
            self.assertNotIn(str(self.tmp), r.text)


class FileLimitTests(FilesApiBase):
    def test_quota_declared_and_streamed(self) -> None:
        with self.app.state.db.transaction() as c:
            c.execute("UPDATE workspaces SET storage_quota_mb = 1 WHERE id = ?", (self.wid,))
        big = b"a" * (2 * 1024 * 1024)
        r = self.put(self.alice, "/big", big)
        self.assertEqual((r.status, r.json()["error"]["code"]), (413, "payload_too_large"))
        # No Content-Length (chunked): caught while streaming, nothing left behind.
        r = self.alice.client.request(
            "PUT",
            f"{self.base}/content?path=/big2",
            chunks=[big[:600_000], big[600_000:1_200_000]],
            headers={"x-csrf-token": self.alice.csrf},
        )
        self.assertEqual(r.status, 413)
        self.assertEqual(os.listdir(self.vol), [])
        self.assertEqual(self.put(self.alice, "/small", b"ok").status, 201)

    def test_global_upload_cap_and_normal_json_cap(self) -> None:
        app = create_app(
            make_settings(self.tmp / "cap", max_upload_mb=1),
            capability_detector=lambda: make_caps({}, tmp=self.tmp),
            password_hasher=fast_hasher(),
            runtimes={"docker": FakeRuntime()},
            job_runner=InlineRunner(),
        )
        from tests.asgi_client import AsgiClient

        (self.tmp / "cap").mkdir(exist_ok=True)
        r = AsgiClient(app).request(
            "PUT", f"/api/v1/workspaces/{self.wid}/files/content?path=/x", content=b"a" * (1024 * 1024 + 10)
        )
        self.assertEqual(r.status, 413)  # rejected by the body limit before any handler work
        # JSON endpoints keep the small default limit even though uploads are larger.
        r = self.alice.post(f"{self.base}/mkdir", json={"path": "a" * (2 * 1024 * 1024)})
        self.assertEqual(r.status, 413)

    def test_validation(self) -> None:
        self.assertEqual(self.alice.post(f"{self.base}/mkdir", json={"path": "/d", "x": 1}).status, 422)
        self.assertEqual(
            self.alice.post(f"{self.base}/mkdir", json={"path": "/a/b"}).status, 404
        )  # no parents
        self.assertEqual(self.alice.get(f"{self.base}?path=/&x=1").status, 200)
        self.assertEqual(self.alice.delete(f"{self.base}?path=/a&recursive=maybe").status, 422)
        self.assertEqual(self.put(self.alice, "").status, 422)


if __name__ == "__main__":
    unittest.main()
