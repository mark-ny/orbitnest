"""Fails when a route is added without authentication or a deliberate PUBLIC_ROUTES entry."""

from __future__ import annotations

import re
import unittest

from starlette.routing import Mount, Route, WebSocketRoute

from app.main import API_PREFIX, PUBLIC_ROUTES
from tests.asgi_client import AsgiClient
from tests.helpers import TempDirMixin


def walk(routes, prefix=""):
    for r in routes:
        if isinstance(r, Mount):
            yield from walk(r.routes, prefix + r.path)
        elif isinstance(r, Route):
            yield prefix + r.path, sorted(m for m in (r.methods or ()) if m not in {"HEAD", "OPTIONS"})


class RouteGuardTests(TempDirMixin, unittest.TestCase):
    def test_every_non_public_route_rejects_anonymous_requests(self) -> None:
        app = self.make_app()
        c = AsgiClient(app)
        seen_public = set()
        checked = 0
        for path, methods in walk(app.routes):
            self.assertTrue(path.startswith(API_PREFIX), path)
            concrete = re.sub(r"\{[a-z_]+\}", "x", path)
            for method in methods:
                if (method, path) in PUBLIC_ROUTES:
                    seen_public.add((method, path))
                    continue
                r = c.request(method, concrete, json={} if method != "GET" else None)
                self.assertEqual(r.status, 401, f"{method} {path} answered anonymous with {r.status}")
                checked += 1
        self.assertEqual(seen_public, set(PUBLIC_ROUTES), "PUBLIC_ROUTES lists a route that does not exist")
        self.assertGreater(checked, 8)

    def test_every_websocket_route_rejects_anonymous_handshakes(self) -> None:
        import asyncio

        from tests import asgi_ws

        app = self.make_app()
        found = []

        def walk_ws(routes, prefix=""):
            for r in routes:
                if isinstance(r, Mount):
                    yield from walk_ws(r.routes, prefix + r.path)
                elif isinstance(r, WebSocketRoute):
                    yield prefix + r.path

        for path in walk_ws(app.routes):
            found.append(path)

            async def attempt(p=re.sub(r"\{[a-z_]+\}", "x", path)):
                c = await asgi_ws.connect(app, p, headers={"origin": "http://localhost"}).start()
                return c.accepted, c.close_code

            self.assertEqual(asyncio.run(attempt()), (False, 1008), path)
        self.assertTrue(found, "no websocket routes found; the guard would silently pass")

    def test_anonymous_requests_carry_no_data(self) -> None:
        app = self.make_app()
        body = AsgiClient(app).get("/api/v1/system/capabilities").text
        self.assertNotIn("platform", body)

    def test_public_surface_is_exactly_what_we_expect(self) -> None:
        self.assertEqual(
            sorted(PUBLIC_ROUTES),
            [
                ("GET", "/api/v1/auth/setup-status"),
                ("GET", "/api/v1/health"),
                ("GET", "/api/v1/ready"),
                ("POST", "/api/v1/auth/login"),
                ("POST", "/api/v1/auth/reset-password"),
                ("POST", "/api/v1/auth/setup"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
