from __future__ import annotations

import unittest

from app.core.platform import detect_capabilities
from app.main import create_app
from tests.asgi_client import AsgiClient
from tests.helpers import TempDirMixin, fast_hasher, make_settings


class ApiFoundationTests(TempDirMixin, unittest.TestCase):
    def test_health(self) -> None:
        r = self.make_client().get("/api/v1/health")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_ready_when_migrated(self) -> None:
        r = self.make_client().get("/api/v1/ready")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["checks"], {"database": True, "migrations": True})

    def test_not_ready_when_migrations_pending(self) -> None:
        app = create_app(make_settings(self.tmp), run_migrations=False)
        r = AsgiClient(app).get("/api/v1/ready")
        self.assertEqual(r.status, 503)
        self.assertFalse(r.json()["checks"]["migrations"])

    def test_capabilities_requires_authentication(self) -> None:
        r = self.make_client().get("/api/v1/system/capabilities")
        self.assertEqual(r.status, 401)
        self.assertEqual(r.json()["error"]["code"], "unauthorized")

    def test_capabilities_for_authenticated_user(self) -> None:
        _, admin = self.admin_session()
        r = admin.get("/api/v1/system/capabilities")
        self.assertEqual(r.status, 200)
        body = r.json()
        self.assertIn(body["mode"], {"linux-docker", "linux-no-container"})
        self.assertIn("features", body)
        self.assertNotIn(str(self.tmp), r.text, "server paths must not leak")

    def test_capabilities_uses_injected_detector(self) -> None:
        caps = detect_capabilities(self.tmp, environ={}, sys_platform="win32", which=lambda n: None)
        app = create_app(
            make_settings(self.tmp), capability_detector=lambda: caps, password_hasher=fast_hasher()
        )
        admin = self.setup_admin(AsgiClient(app))
        self.assertEqual(admin.get("/api/v1/system/capabilities").json()["mode"], "windows-no-container")


class ErrorSchemaTests(TempDirMixin, unittest.TestCase):
    def assert_error(self, r, status: int, code: str) -> dict:
        self.assertEqual(r.status, status)
        err = r.json()["error"]
        self.assertEqual(err["code"], code)
        self.assertTrue(err["message"])
        self.assertEqual(err["request_id"], r.headers["x-request-id"])
        return err

    def test_not_found(self) -> None:
        self.assert_error(self.make_client().get("/api/v1/nope"), 404, "not_found")

    def test_method_not_allowed(self) -> None:
        self.assert_error(self.make_client().post("/api/v1/health"), 405, "method_not_allowed")

    def test_unhandled_exception_is_generic_500_with_headers(self) -> None:
        def boom() -> object:
            raise RuntimeError("secret internal detail /srv/app/path password=hunter2")

        app = create_app(make_settings(self.tmp), capability_detector=boom, password_hasher=fast_hasher())
        admin = self.setup_admin(AsgiClient(app))
        r = admin.get("/api/v1/system/capabilities")
        err = self.assert_error(r, 500, "internal_error")
        self.assertNotIn("hunter2", r.text)
        self.assertNotIn("/srv/app", r.text)
        self.assertEqual(err["message"], "An internal error occurred.")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_client_supplied_request_id_is_echoed_when_valid(self) -> None:
        r = self.make_client().get("/api/v1/health", headers={"X-Request-ID": "abc-12345678"})
        self.assertEqual(r.headers["x-request-id"], "abc-12345678")

    def test_malicious_request_id_is_replaced(self) -> None:
        r = self.make_client().get("/api/v1/health", headers={"X-Request-ID": "x\r\nSet-Cookie: a=b"})
        self.assertNotIn("Set-Cookie", r.headers["x-request-id"])
        self.assertRegex(r.headers["x-request-id"], r"^[0-9a-f]{32}$")


class SecurityMiddlewareTests(TempDirMixin, unittest.TestCase):
    def test_security_headers_present(self) -> None:
        h = self.make_client().get("/api/v1/health").headers
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        self.assertNotIn("unsafe-inline", h["content-security-policy"])
        self.assertEqual(h["cache-control"], "no-store")
        self.assertNotIn("strict-transport-security", h)

    def test_hsts_only_on_https(self) -> None:
        app = create_app(make_settings(self.tmp))
        h = AsgiClient(app, scheme="https").get("/api/v1/health").headers
        self.assertIn("strict-transport-security", h)

    def test_forwarded_proto_ignored_unless_proxy_trusted(self) -> None:
        h = self.make_client().get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers
        self.assertNotIn("strict-transport-security", h)
        trusted = self.make_client(trusted_proxy=True)
        h2 = trusted.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers
        self.assertIn("strict-transport-security", h2)

    def test_unexpected_host_rejected(self) -> None:
        r = self.make_client().get("/api/v1/health", host="evil.example")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_host")

    def test_loopback_hosts_with_port_accepted(self) -> None:
        c = self.make_client()
        for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertEqual(c.get("/api/v1/health", host=host).status, 200)

    def test_public_url_host_accepted(self) -> None:
        c = self.make_client(public_url="https://nest.example.com")
        self.assertEqual(c.get("/api/v1/health", host="nest.example.com").status, 200)

    def test_body_limit_by_content_length(self) -> None:
        c = self.make_client(max_request_bytes=1024)
        r = c.post("/api/v1/health", content=b"x" * 2048)
        self.assertEqual(r.status, 413)
        self.assertEqual(r.json()["error"]["code"], "payload_too_large")

    def test_body_limit_when_streaming_without_content_length(self) -> None:
        # Route 405s without reading the body, so mount a probe that does read it.
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def sink(request: Request) -> JSONResponse:
            return JSONResponse({"n": len(await request.body())})

        app = create_app(make_settings(self.tmp, max_request_bytes=1024))
        app.router.routes.append(Route("/sink", sink, methods=["POST"]))
        c = AsgiClient(app)
        r = c.post("/sink", chunks=[b"x" * 700, b"x" * 700])
        self.assertEqual(r.status, 413)
        ok = c.post("/sink", chunks=[b"x" * 300, b"x" * 300])
        self.assertEqual(ok.json(), {"n": 600})

    def test_cors_not_enabled_by_default_and_exact_when_configured(self) -> None:
        r = self.make_client().get("/api/v1/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        c = self.make_client(cors_origins=["https://app.example"])
        good = c.get("/api/v1/health", headers={"Origin": "https://app.example"})
        self.assertEqual(good.headers["access-control-allow-origin"], "https://app.example")
        bad = c.get("/api/v1/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", bad.headers)


if __name__ == "__main__":
    unittest.main()
