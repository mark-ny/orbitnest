"""Terminal WebSocket: ``/api/v1/workspaces/{id}/terminal``.

Protocol. Client -> server: binary frames are keystrokes; text frames are JSON control messages
``{"type":"resize","cols":N,"rows":N}`` or ``{"type":"ping"}``. Server -> client: binary frames are
terminal output; text frames are JSON ``{"type":"ready"|"exit"|"closed"|"error"|"pong", ...}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.auth.deps import authenticate_ws, request_context
from app.auth.policy import Perm
from app.core import errors
from app.runtimes.pty_shell import clamp_size
from app.services.terminals import TerminalService, TerminalSession

log = logging.getLogger("orbitnest.terminal")

MAX_INPUT_FRAME = 64 * 1024
MAX_CONTROL_FRAME = 1024
TOUCH_INTERVAL = 60.0


def _size(ws: WebSocket) -> tuple[int, int]:
    def num(name: str, default: int) -> int:
        try:
            return int(ws.query_params.get(name, default))
        except ValueError:
            return default

    return clamp_size(num("cols", 80), num("rows", 24))


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(payload, separators=(",", ":")))


async def terminal(ws: WebSocket) -> None:
    try:
        auth = authenticate_ws(ws, permission=Perm.WORKSPACE_WRITE)
    except errors.AppError:
        await ws.close(code=1008)  # before accept: the browser sees a failed handshake
        return
    settings = ws.app.state.settings
    svc: TerminalService = ws.app.state.terminals
    wid = ws.path_params["workspace_id"]
    cols, rows = _size(ws)
    try:
        session = await run_in_threadpool(svc.open, auth.user, wid, cols, rows, request_context(ws))
    except errors.AppError as exc:
        await ws.accept()
        await _send_json(ws, {"type": "error", "code": exc.code, "message": exc.message})
        await ws.close(code=1008)
        return

    await ws.accept()
    await _send_json(ws, {"type": "ready"})
    reason = "closed"
    try:
        reason = await _pump(ws, session, svc, auth.user, settings)
    except (WebSocketDisconnect, RuntimeError):
        reason = "client disconnected"
    except Exception:
        log.exception("terminal crashed")
        reason = "error"
    finally:
        await run_in_threadpool(session.close, reason)
        with contextlib.suppress(RuntimeError):  # already closed
            await ws.close()


async def _pump(
    ws: WebSocket, session: TerminalSession, svc: TerminalService, user: Any, settings: Any
) -> str:
    state = {"last_input": time.monotonic(), "last_touch": 0.0, "dirty": False}
    idle_limit = settings.terminal_idle_minutes * 60
    max_life = settings.terminal_max_hours * 3600

    async def output() -> str:
        while True:
            now = time.monotonic()
            if now - state["last_input"] > idle_limit:
                await _send_json(ws, {"type": "closed", "reason": "idle"})
                return "idle timeout"
            if now - session.opened > max_life:
                await _send_json(ws, {"type": "closed", "reason": "time limit"})
                return "time limit"
            if state["dirty"] and now - state["last_touch"] > TOUCH_INTERVAL:
                state["dirty"], state["last_touch"] = False, now
                with contextlib.suppress(errors.AppError):
                    await run_in_threadpool(svc.workspaces.touch_activity, user, session.workspace_id)
            data = await run_in_threadpool(session.shell.read, 0.5)
            if data is None:
                continue
            if data == b"":
                await _send_json(ws, {"type": "exit", "code": session.shell.exit_code})
                return "shell exited"
            session.bytes_out += len(data)
            await ws.send_bytes(data)

    async def inputs() -> str:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return "client disconnected"
            raw = msg.get("bytes")
            if raw is not None:
                if len(raw) > MAX_INPUT_FRAME:
                    await ws.close(code=1009)
                    return "frame too large"
                session.bytes_in += len(raw)
                state["last_input"], state["dirty"] = time.monotonic(), True
                await run_in_threadpool(session.shell.write, raw)
                continue
            text = msg.get("text") or ""
            if len(text) > MAX_CONTROL_FRAME:
                await ws.close(code=1009)
                return "frame too large"
            try:
                ctl = json.loads(text)
            except ValueError:
                continue
            kind = ctl.get("type") if isinstance(ctl, dict) else None
            if kind == "resize":
                try:
                    c, r = clamp_size(int(ctl.get("cols", 80)), int(ctl.get("rows", 24)))
                except (TypeError, ValueError):
                    continue
                await run_in_threadpool(session.shell.resize, c, r)
            elif kind == "ping":
                state["last_input"] = time.monotonic()  # a visible tab keeps the session alive
                await _send_json(ws, {"type": "pong"})

    tasks = [asyncio.ensure_future(output()), asyncio.ensure_future(inputs())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    first = next(iter(done))
    return "closed" if first.cancelled() else first.result()


routes = [WebSocketRoute("/workspaces/{workspace_id}/terminal", terminal)]
