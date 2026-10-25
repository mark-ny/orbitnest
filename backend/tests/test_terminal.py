"""Terminal WebSocket at the ASGI level (see tests/asgi_ws.py for what that does and does not prove)."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.services.audit import list_events
from tests import asgi_ws
from tests.helpers import ServiceHarness, make_caps
from tests.test_workspace_api import ApiBase

ORIGIN = {"origin": "http://localhost"}


def run(coro):
    return asyncio.run(coro)


class TerminalApiBase(ApiBase):
    def setUp(self) -> None:
        super().setUp()
        self.wid = self.create(name="term").json()["id"]
        self.path = f"/api/v1/workspaces/{self.wid}/terminal"

    def conn(self, who, headers=None, path=None):
        return asgi_ws.connect(
            self.app, path or self.path, cookies=who.client.cookies, headers={**ORIGIN, **(headers or {})}
        )

    def audit_actions(self, prefix="terminal."):
        events, _ = list_events(self.app.state.db, limit=100, cursor=None, action_prefix=prefix)
        return [e["action"] for e in events]


class TerminalSessionTests(TerminalApiBase):
    def test_full_session(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            self.assertTrue(c.accepted)
            self.assertEqual(json.loads((await c.recv())[1]), {"type": "ready"})
            self.assertIn(b"$ ", await c.recv_until(b"$ "))
            await c.send_bytes(b"ls\r")
            self.assertIn(b"ls\r", await c.recv_until(b"ls\r"))
            await c.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            await c.send_text(json.dumps({"type": "ping"}))
            kind, payload = await c.recv()
            self.assertEqual((kind, json.loads(payload)), ("text", {"type": "pong"}))
            await c.send_bytes(b"exit\n")
            seen = []
            for _ in range(5):
                kind, payload = await c.recv()
                if kind == "text":
                    seen.append(json.loads(payload))
                if kind in ("close", "done"):
                    break
            self.assertIn({"type": "exit", "code": 0}, seen)
            await c.disconnect()

        run(scenario())
        shell = self.fake.shells[0]
        self.assertEqual(shell.resizes, [(120, 40)])
        self.assertTrue(shell.closed)
        self.assertEqual(self.app.state.terminals.active(), 0)
        self.assertEqual(sorted(self.audit_actions()), ["terminal.close", "terminal.open"])

    def test_client_disconnect_releases_everything(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            await c.recv()
            await c.disconnect()

        run(scenario())
        self.assertTrue(self.fake.shells[0].closed)
        self.assertEqual(self.app.state.terminals.active(), 0)

    def test_malformed_and_oversized_input(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            await c.recv()
            for bad in (
                "not json",
                "[]",
                '{"type":"resize","cols":"x"}',
                '{"type":"resize","cols":1e99}',
                '{"type":"nope"}',
            ):
                await c.send_text(bad)
            await c.send_bytes(b"still-alive\r")
            self.assertIn(b"still-alive", await c.recv_until(b"still-alive"))
            await c.send_bytes(b"x" * (64 * 1024 + 1))
            for _ in range(20):
                kind, payload = await c.recv()
                if kind == "close":
                    self.assertEqual(payload, 1009)
                    break
            else:
                self.fail("oversized frame did not close the socket")
            await c.disconnect()

        run(scenario())
        self.assertEqual(self.fake.shells[0].resizes[:1], [(500, 24)])
        self.assertEqual(self.app.state.terminals.active(), 0)

    def test_stopping_the_workspace_ends_terminals(self) -> None:
        async def scenario():
            c = await self.conn(self.alice).start()
            await c.recv()
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.alice.post(f"/api/v1/workspaces/{self.wid}/stop")
            )
            kinds = []
            for _ in range(10):
                kind, payload = await c.recv()
                kinds.append(kind if kind != "text" else json.loads(payload)["type"])
                if kind in ("close", "done"):
                    break
            self.assertIn("exit", kinds)
            await c.disconnect()

        run(scenario())
        self.assertEqual(self.app.state.terminals.active(), 0)
        self.assertTrue(self.fake.shells[0].closed)

    def test_idle_timeout(self) -> None:
        clock = {"t": 1000.0}

        class FakeTime:
            @staticmethod
            def monotonic() -> float:
                return clock["t"]

        async def scenario():
            c = await self.conn(self.alice).start()
            await c.recv()
            clock["t"] += 31 * 60
            msgs = []
            for _ in range(10):
                kind, payload = await c.recv()
                if kind == "text":
                    msgs.append(json.loads(payload))
                if kind in ("close", "done") or any(m["type"] == "closed" for m in msgs):
                    break
            self.assertIn({"type": "closed", "reason": "idle"}, msgs)
            await c.disconnect()

        with mock.patch("app.api.terminal.time", FakeTime):
            run(scenario())
        self.assertEqual(self.app.state.terminals.active(), 0)


class TerminalSecurityTests(TerminalApiBase):
    def rejected_before_accept(self, conn) -> bool:
        async def go():
            c = await conn.start()
            return (not c.accepted) and c.close_code == 1008

        return run(go())

    def test_anonymous_rejected(self) -> None:
        self.assertTrue(self.rejected_before_accept(asgi_ws.connect(self.app, self.path, headers=ORIGIN)))
        self.assertEqual(self.fake.shells, [])

    def test_cross_site_websocket_hijack_rejected_and_audited(self) -> None:
        for origin in ("https://evil.example", "null", "http://localhost.evil.example"):
            self.assertTrue(self.rejected_before_accept(self.conn(self.alice, {"origin": origin})), origin)
        self.assertEqual(self.fake.shells, [])
        self.assertIn("authz.ws_origin_rejected", self.audit_actions("authz."))

    def test_bad_session_cookie_rejected(self) -> None:
        conn = asgi_ws.connect(self.app, self.path, cookies={"orbitnest_session": "forged"}, headers=ORIGIN)
        self.assertTrue(self.rejected_before_accept(conn))

    def test_invalid_host_rejected(self) -> None:
        async def go():
            c = await self.conn(self.alice, {"host": "evil.example"}).start()
            return c.accepted

        self.assertFalse(run(go()))

    def test_other_users_workspace_is_not_found(self) -> None:
        async def go():
            c = await self.conn(self.bob).start()
            self.assertTrue(c.accepted)
            kind, payload = await c.recv()
            msg = json.loads(payload)
            self.assertEqual((msg["type"], msg["code"]), ("error", "not_found"))
            kind, code = await c.recv()
            self.assertEqual((kind, code), ("close", 1008))

        run(go())
        self.assertEqual(self.fake.shells, [])

    def test_read_only_role_denied(self) -> None:
        self.assertTrue(self.rejected_before_accept(self.conn(self.viewer)))

    def test_stopped_workspace_gives_error(self) -> None:
        self.alice.post(f"/api/v1/workspaces/{self.wid}/stop")

        async def go():
            c = await self.conn(self.alice).start()
            msg = json.loads((await c.recv())[1])
            self.assertEqual(msg["code"], "workspace_busy")

        run(go())

    def test_admin_can_open_foreign_terminal(self) -> None:
        async def go():
            c = await self.conn(self.admin).start()
            self.assertEqual(json.loads((await c.recv())[1]), {"type": "ready"})
            await c.disconnect()

        run(go())


class TerminalLimitTests(TerminalApiBase):
    settings_overrides = {"max_terminals_per_workspace": 2, "max_terminals_per_user": 3}  # noqa: RUF012

    def test_per_workspace_and_per_user_limits(self) -> None:
        other = self.create(name="other").json()["id"]  # sync client: must not run inside the loop

        async def go():
            a = await self.conn(self.alice).start()
            b = await self.conn(self.alice).start()
            await a.recv()
            await b.recv()
            third = await self.conn(self.alice).start()
            msg = json.loads((await third.recv())[1])
            self.assertEqual(msg["code"], "session_limit")
            # per-user limit across workspaces
            c = await self.conn(self.alice, path=f"/api/v1/workspaces/{other}/terminal").start()
            self.assertEqual(json.loads((await c.recv())[1]), {"type": "ready"})
            d = await self.conn(self.alice, path=f"/api/v1/workspaces/{other}/terminal").start()
            self.assertEqual(json.loads((await d.recv())[1])["code"], "session_limit")
            for x in (a, b, c):
                await x.disconnect()
            e = await self.conn(self.alice).start()
            self.assertEqual(json.loads((await e.recv())[1]), {"type": "ready"})
            await e.disconnect()

        run(go())
        self.assertEqual(self.app.state.terminals.active(), 0)


class LocalRuntimeTerminalTests(ServiceHarness):
    """Termux model: a terminal is a host shell, so only administrators may open one."""

    def test_local_terminal_is_admin_only(self) -> None:
        from app.core import errors
        from app.runtimes.termux_runtime import LocalProcessRuntime
        from app.services.terminals import TerminalService

        self.caps = make_caps({"TERMUX_VERSION": "1"}, docker=False, tmp=self.tmp)
        local = LocalProcessRuntime(self.tmp / "rt")
        svc = self.new_service(runtimes={"local": local})
        terms = TerminalService(svc, self.settings)
        ws, _ = svc.create(self.alice, name="py", template_id="dev-python")
        self.assertEqual((ws.runtime, svc.get(self.alice, ws.id).status), ("local", "running"))
        with self.assertRaises(errors.AppError) as cm:
            terms.open(self.alice, ws.id, 80, 24)
        self.assertEqual((cm.exception.code, cm.exception.status_code), ("forbidden", 403))
        self.assertEqual(terms.active(), 0)
        session = terms.open(self.admin, ws.id, 80, 24)
        self.assertEqual(terms.active(), 1)
        svc.stop(self.alice, ws.id)  # hook must kill the shell too
        self.assertEqual(terms.active(), 0)
        self.assertEqual(session.shell.read(3.0), b"")  # the shell was killed, not just forgotten
        for rid in list(local.list_managed()):
            local.remove(rid)


if __name__ == "__main__":
    unittest.main()
