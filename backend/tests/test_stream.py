"""Streaming gateway at the ASGI level, with real subprocesses behind the fake runtime.

The relay is a real child process (an echo peer), so pipe handling, kill-on-close and limits are
exercised for real. What is NOT proven: a real VNC server, noVNC rendering, or a real WebSocket
wire protocol (see tests/asgi_ws.py).
"""

from __future__ import annotations

import asyncio
import unittest

from app.services.audit import list_events
from tests import asgi_ws
from tests.test_workspace_api import ApiBase

ORIGIN = {"origin": "http://localhost"}


def run(coro):
    return asyncio.run(coro)


class StreamBase(ApiBase):
    def setUp(self) -> None:
        super().setUp()
        self.wid = self.create(name="screen", template_id="browser-chromium").json()["id"]
        self.base = f"/api/v1/workspaces/{self.wid}/stream"

    def conn(self, who, headers=None, wid=None):
        return asgi_ws.connect(
            self.app,
            f"/api/v1/workspaces/{wid or self.wid}/stream/ws",
            cookies=who.client.cookies,
            headers={**ORIGIN, **(headers or {})},
            subprotocols=["binary"],
        )

    def actions(self):
        events, _ = list_events(self.app.state.db, limit=100, cursor=None, action_prefix="stream.")
        return [e["action"] for e in events]


class AssetTests(StreamBase):
    def test_owner_gets_allowlisted_asset_with_safe_headers(self) -> None:
        r = self.alice.get(f"{self.base}/assets/core/rfb.js")
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.text, "// fake asset core/rfb.js")
        h = {k.lower(): v for k, v in r.headers.items()}
        self.assertTrue(h["content-type"].startswith("text/javascript"))
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("private", h["cache-control"])

    def test_second_request_is_served_from_cache(self) -> None:
        for _ in range(3):
            self.assertEqual(self.alice.get(f"{self.base}/assets/core/rfb.js").status, 200)
        self.assertEqual(len(self.fake.ops("asset")), 1)

    def test_only_allowlisted_paths(self) -> None:
        for bad in (
            "core/rfb.txt",
            "app/ui.js",
            "core/../../etc/passwd.js",
            "core/%2e%2e/x.js",
            "core//x.js",
            "vendor/",
            "core/a b.js",
            "../core/rfb.js",
            "core/rfb.js%00",
        ):
            r = self.alice.get(f"{self.base}/assets/{bad}")
            self.assertIn(r.status, (404, 400, 422), bad)
        self.assertEqual(self.fake.ops("asset"), [])

    def test_authorization(self) -> None:
        url = f"{self.base}/assets/core/rfb.js"
        self.assertEqual(self.bob.get(url).status, 404)  # not the owner: indistinguishable from missing
        self.assertEqual(self.viewer.get(url).status, 403)  # read-only role may not drive a screen
        from tests.asgi_client import AsgiClient

        self.assertEqual(AsgiClient(self.app).get(url).status, 401)
        self.assertEqual(self.admin.get(url).status, 200)  # administrators may

    def test_non_stream_workspace_and_stopped_workspace(self) -> None:
        dev = self.create(name="dev").json()["id"]
        r = self.alice.get(f"/api/v1/workspaces/{dev}/stream/assets/core/rfb.js")
        self.assertEqual((r.status, r.json()["error"]["code"]), (409, "not_supported"))
        self.assertEqual(self.alice.post(f"/api/v1/workspaces/{self.wid}/stop").status, 202)
        r = self.alice.get(f"{self.base}/assets/core/rfb.js")
        self.assertEqual(r.status, 409)


class RelayTests(StreamBase):
    def test_bytes_round_trip_through_a_real_subprocess(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            self.assertTrue(c.accepted)
            payload = bytes(range(256)) * 40  # binary-clean, larger than one pipe read
            await c.send_bytes(payload)
            got = await c.recv_until(payload, timeout=10)
            self.assertEqual(got, payload)
            await c.disconnect()

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)
        self.assertEqual(sorted(self.actions()), ["stream.close", "stream.open"])

    def test_origin_and_auth_enforced(self) -> None:
        async def scenario():
            c = await self.conn(self.alice, headers={"origin": "http://evil.example"}).start()
            self.assertFalse(c.accepted)
            c = await asgi_ws.connect(
                self.app, f"{self.base}/ws", headers=ORIGIN, subprotocols=["binary"]
            ).start()
            self.assertFalse(c.accepted)
            c = await self.conn(self.bob).start()  # someone else's workspace
            self.assertFalse(c.accepted)
            c = await self.conn(self.viewer).start()
            self.assertFalse(c.accepted)

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)
        self.assertEqual(self.fake.ops("stream_bridge"), [])

    def test_text_frames_rejected(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            await c.send_text("hello")
            kind, code = await c.recv()
            self.assertEqual((kind, code), ("close", 1003))

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)

    def test_oversized_frame_closes(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            await c.send_bytes(b"x" * (1024 * 1024 + 1))
            kind, code = await c.recv()
            self.assertEqual((kind, code), ("close", 1009))

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)

    def test_per_workspace_and_per_user_limits(self) -> None:
        async def scenario():
            a = await self.conn(self.alice).start()
            b = await self.conn(self.alice).start()
            self.assertTrue(a.accepted and b.accepted)
            c = await self.conn(self.alice).start()  # default limit: 2 per workspace
            self.assertFalse(c.accepted)
            await a.disconnect()
            d = await self.conn(self.alice).start()  # a slot is free again
            self.assertTrue(d.accepted)
            await b.disconnect()
            await d.disconnect()

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)

    def test_stopping_the_workspace_ends_the_stream_and_kills_the_bridge(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            self.assertTrue(c.accepted)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.alice.post(f"/api/v1/workspaces/{self.wid}/stop")
            )
            for _ in range(50):
                kind, _payload = await c.recv()
                if kind in ("close", "done"):
                    break
            else:
                self.fail("stream stayed open after the workspace stopped")

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)

    def test_max_lifetime(self) -> None:
        self.app.state.settings = self.app.state.settings.model_copy(update={"stream_max_hours": 1})

        async def scenario():
            import app.api.stream as mod

            real = asyncio.sleep

            async def fast_sleep(seconds, *a, **kw):
                await real(0.05 if seconds >= 3600 else seconds, *a, **kw)

            mod.asyncio.sleep = fast_sleep
            try:
                c = await self.conn(self.alice).start()
                for _ in range(50):
                    kind, _p = await c.recv()
                    if kind in ("close", "done"):
                        break
            finally:
                mod.asyncio.sleep = real

        run(scenario())
        self.assertEqual(self.app.state.streams.active(), 0)


if __name__ == "__main__":
    unittest.main()
