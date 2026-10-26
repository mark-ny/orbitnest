"""Screen streaming gateway for browser/desktop workspaces.

* ``GET  /workspaces/{id}/stream/assets/{path}``  the noVNC client files (allowlisted, cached)
* ``WS   /workspaces/{id}/stream/ws``             binary relay to the in-container VNC server

Both require the session cookie and workspace write access (owner or admin); the WebSocket also
checks Origin. The relay is a byte pipe to ``docker exec -i ... socat``; no host port is opened.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.auth.deps import authenticate, authenticate_ws, request_context
from app.auth.policy import Perm
from app.core import errors
from app.services.streams import MAX_ASSET_BYTES, StreamService, StreamSession

log = logging.getLogger("orbitnest.stream")

MAX_CLIENT_FRAME = 1024 * 1024
READ_CHUNK = 64 * 1024
ASSET_TIMEOUT = 15.0


async def _run_asset(argv: list[str], env: dict[str, str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert proc.stdout is not None
        data = await asyncio.wait_for(proc.stdout.read(MAX_ASSET_BYTES + 1), ASSET_TIMEOUT)
        code = await asyncio.wait_for(proc.wait(), ASSET_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise errors.AppError(
            errors.WORKSPACE_BUSY, "The viewer file could not be read in time.", 504
        ) from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if code != 0 or not data or len(data) > MAX_ASSET_BYTES:
        raise errors.AppError(errors.NOT_FOUND, "Viewer file not found.", 404)
    return data


async def asset(request: Request) -> Response:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    svc: StreamService = request.app.state.streams
    path = request.path_params["path"]
    if not svc.valid_asset(path):
        raise errors.AppError(errors.NOT_FOUND, "Viewer file not found.", 404)
    target = await run_in_threadpool(
        svc.target, auth.user, request.path_params["workspace_id"], request_context(request)
    )
    data = svc.cache_get(target.image, path)
    if data is None:
        argv, env = svc.asset_command(target, path)
        data = await _run_asset(argv, env)
        svc.cache_put(target.image, path, data)
    return Response(
        data,
        media_type="text/javascript",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


async def _relay(
    ws: WebSocket, session: StreamSession, proc: asyncio.subprocess.Process, max_seconds: int
) -> str:
    assert proc.stdin is not None and proc.stdout is not None

    async def downstream() -> str:  # container -> browser
        while True:
            chunk = await proc.stdout.read(READ_CHUNK)  # type: ignore[union-attr]
            if not chunk:
                return "screen server ended"
            session.bytes_out += len(chunk)
            await ws.send_bytes(chunk)

    async def upstream() -> str:  # browser -> container
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return "client disconnected"
            raw = msg.get("bytes")
            if raw is None:
                await ws.close(code=1003)  # the VNC relay is binary-only
                return "text frame rejected"
            if len(raw) > MAX_CLIENT_FRAME:
                await ws.close(code=1009)
                return "frame too large"
            session.bytes_in += len(raw)
            proc.stdin.write(raw)  # type: ignore[union-attr]
            await proc.stdin.drain()  # type: ignore[union-attr]

    async def lifetime() -> str:
        await asyncio.sleep(max_seconds)
        return "time limit"

    tasks = [asyncio.ensure_future(t()) for t in (downstream, upstream, lifetime)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    first = next(iter(done))
    if first.cancelled():
        return "closed"
    exc = first.exception()
    return (
        "client disconnected"
        if isinstance(exc, WebSocketDisconnect | ConnectionError | RuntimeError)
        else (first.result() if exc is None else "error")
    )


async def stream_ws(ws: WebSocket) -> None:
    try:
        auth = authenticate_ws(ws, permission=Perm.WORKSPACE_WRITE)
    except errors.AppError:
        await ws.close(code=1008)
        return
    svc: StreamService = ws.app.state.streams
    try:
        session, target = await run_in_threadpool(
            svc.open, auth.user, ws.path_params["workspace_id"], request_context(ws)
        )
    except errors.AppError:
        await ws.close(code=1008)
        return
    proc: asyncio.subprocess.Process | None = None
    reason = "closed"
    try:
        argv, env = svc.bridge_command(target)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        loop = asyncio.get_running_loop()

        def kill_now() -> None:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()

        def kill() -> None:  # called from any thread
            with contextlib.suppress(RuntimeError):  # event loop already closed
                loop.call_soon_threadsafe(kill_now)

        session.on_close.append(kill)
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol="binary" if "binary" in offered else None)
        reason = await _relay(ws, session, proc, ws.app.state.settings.stream_max_hours * 3600)
    except errors.AppError:
        reason = "unavailable"
    except Exception:
        log.exception("stream crashed")
        reason = "error"
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        await run_in_threadpool(session.close, reason)
        with contextlib.suppress(RuntimeError):
            await ws.close()


routes = [
    Route("/workspaces/{workspace_id}/stream/assets/{path:path}", asset, methods=["GET"]),
    WebSocketRoute("/workspaces/{workspace_id}/stream/ws", stream_ws),
]
