"""Streamed workspaces (browsers and desktops): authorization, limits, viewer assets, audit.

How the picture reaches the user without opening any host port:

* The container runs a VNC server bound to ``127.0.0.1`` *inside* the container.
* For each viewer the control plane starts ``docker exec -i <container> socat - TCP:127.0.0.1:5900``
  and relays WebSocket binary frames to that process's stdin and stdout back. Nothing listens on
  the host, so no other local process or network peer can reach the VNC server.
* The noVNC client is read from a fixed, root-owned path in the image (``docker exec cat``) and
  served same-origin from a strict allowlist, so the app's CSP (no inline script) stays intact.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from app.core import errors
from app.core.config import Settings
from app.runtimes.base import RuntimeFailure
from app.services import audit, lifecycle
from app.services.audit import RequestContext
from app.services.workspaces import WorkspaceService

if TYPE_CHECKING:
    from app.auth.service import User

ASSET_RE = re.compile(r"^(core|vendor)/[A-Za-z0-9._/-]{1,150}\.js\Z")
MAX_ASSET_BYTES = 2 * 1024 * 1024

# Debian's novnc package omits this tiny pako module although core/inflator.js and deflator.js import
# it, which breaks the whole viewer. It is a plain data holder (pako 2.x, MIT licence), so the control
# plane supplies it only when the image does not have it. Keyed by exact path.
FALLBACK_ASSETS: dict[str, bytes] = {
    "vendor/pako/lib/zlib/zstream.js": b"""// Supplied by OrbitNest (pako, MIT); absent from the distro package.
function ZStream() {
  /* next input byte */
  this.input = null; // JS specific, because we have no pointers
  this.next_in = 0;
  /* number of bytes available at input */
  this.avail_in = 0;
  /* total number of input bytes read so far */
  this.total_in = 0;
  /* next output byte should be put there */
  this.output = null; // JS specific, because we have no pointers
  this.next_out = 0;
  /* remaining free space at output */
  this.avail_out = 0;
  /* total number of bytes output so far */
  this.total_out = 0;
  /* last error message, NULL if no error */
  this.msg = ''/*Z_NULL*/;
  /* not visible by applications */
  this.state = null;
  /* best guess about the data type: binary or text */
  this.data_type = 2/*Z_UNKNOWN*/;
  /* adler32 value of the uncompressed data */
  this.adler = 0;
}

export default ZStream;
""",
}
CACHE_ENTRIES = 256
CACHE_BYTES = 24 * 1024 * 1024


class StreamSession:
    def __init__(self, service: StreamService, workspace_id: str, user_id: str) -> None:
        self.service = service
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.opened = time.monotonic()
        self.bytes_in = 0
        self.bytes_out = 0
        self.on_close: list = []
        self._closed = False
        self._lock = threading.Lock()

    def close(self, reason: str = "closed") -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for hook in self.on_close:
            with contextlib.suppress(Exception):  # best effort teardown
                hook()
        self.service._release(self, reason)


class StreamTarget:
    """Everything the transport needs, resolved after authorization."""

    def __init__(self, workspace_id: str, resource_id: str, adapter, port: int, image: str) -> None:  # type: ignore[no-untyped-def]
        self.workspace_id = workspace_id
        self.resource_id = resource_id
        self.adapter = adapter
        self.port = port
        self.image = image


class StreamService:
    def __init__(self, workspaces: WorkspaceService, settings: Settings) -> None:
        self.workspaces = workspaces
        self.settings = settings
        self._lock = threading.Lock()
        self._sessions: set[StreamSession] = set()
        self._pending: dict[str, int] = {}
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_bytes = 0
        workspaces.on_inactive.append(self.close_workspace)

    def active(self, workspace_id: str | None = None) -> int:
        with self._lock:
            return sum(1 for s in self._sessions if workspace_id in (None, s.workspace_id))

    # -- authorization --------------------------------------------------------------------
    def target(self, actor: User, workspace_id: str, ctx: RequestContext | None = None) -> StreamTarget:
        ws = self.workspaces.access(actor, workspace_id, write=True, ctx=ctx)
        template = self.workspaces.registry.get(ws.template_id)
        if template is None or template.access != "stream" or template.stream_port is None:
            raise errors.AppError(errors.NOT_SUPPORTED, "This workspace has no screen to stream.", 409)
        if ws.status not in lifecycle.LIVE or ws.status != "running":
            raise errors.AppError(
                errors.WORKSPACE_BUSY,
                f"The workspace is {ws.status}. Start it before opening the screen.",
                409,
            )
        adapter = self.workspaces.adapters().get(ws.runtime)
        inst = self.workspaces.store.current_instance(ws.id)
        if adapter is None or inst is None or adapter.isolation != "container":
            raise errors.AppError(errors.WORKSPACE_BUSY, "The workspace has no running instance.", 409)
        return StreamTarget(ws.id, inst.resource_id, adapter, template.stream_port, template.image)

    def open(
        self, actor: User, workspace_id: str, ctx: RequestContext | None = None
    ) -> tuple[StreamSession, StreamTarget]:
        target = self.target(actor, workspace_id, ctx)
        with self._lock:
            per_ws = sum(1 for s in self._sessions if s.workspace_id == target.workspace_id)
            per_ws += self._pending.get(f"ws:{target.workspace_id}", 0)
            per_user = sum(1 for s in self._sessions if s.user_id == actor.id)
            per_user += self._pending.get(f"user:{actor.id}", 0)
            if per_ws >= self.settings.max_streams_per_workspace:
                raise errors.AppError(
                    errors.SESSION_LIMIT, "This workspace already has the maximum number of viewers.", 409
                )
            if per_user >= self.settings.max_streams_per_user:
                raise errors.AppError(
                    errors.SESSION_LIMIT, "You already have the maximum number of open screens.", 409
                )
            session = StreamSession(self, target.workspace_id, actor.id)
            self._sessions.add(session)
        audit.record(
            self.workspaces.db,
            action="stream.open",
            outcome="success",
            actor_user_id=actor.id,
            target_type="workspace",
            target_id=target.workspace_id,
            ctx=ctx,
        )
        return session, target

    def _release(self, session: StreamSession, reason: str) -> None:
        with self._lock:
            self._sessions.discard(session)
        audit.record(
            self.workspaces.db,
            action="stream.close",
            outcome="success",
            actor_user_id=session.user_id,
            target_type="workspace",
            target_id=session.workspace_id,
            details={
                "reason": reason,
                "seconds": int(time.monotonic() - session.opened),
                "bytes_in": session.bytes_in,
                "bytes_out": session.bytes_out,
            },
        )

    def close_workspace(self, workspace_id: str) -> None:
        with self._lock:
            victims = [s for s in self._sessions if s.workspace_id == workspace_id]
        for s in victims:
            s.close("workspace stopped")

    def close_all(self) -> None:
        with self._lock:
            victims = list(self._sessions)
        for s in victims:
            s.close("server shutdown")

    # -- assets ---------------------------------------------------------------------------
    @staticmethod
    def valid_asset(path: str) -> bool:
        return bool(ASSET_RE.match(path)) and ".." not in path.split("/") and "//" not in path

    def cache_get(self, image: str, path: str) -> bytes | None:
        with self._lock:
            data = self._cache.get((image, path))
            if data is not None:
                self._cache.move_to_end((image, path))
            return data

    def cache_put(self, image: str, path: str, data: bytes) -> None:
        with self._lock:
            key = (image, path)
            if key in self._cache:
                return
            self._cache[key] = data
            self._cache_bytes += len(data)
            while len(self._cache) > CACHE_ENTRIES or self._cache_bytes > CACHE_BYTES:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= len(old)

    def asset_command(self, target: StreamTarget, path: str) -> tuple[list[str], dict[str, str]]:
        try:
            return target.adapter.asset_argv(target.resource_id, path)
        except RuntimeFailure as exc:
            raise errors.AppError(errors.WORKSPACE_BUSY, exc.safe_message, 409) from None

    def bridge_command(self, target: StreamTarget) -> tuple[list[str], dict[str, str]]:
        try:
            return target.adapter.stream_bridge_argv(target.resource_id, target.port)
        except RuntimeFailure as exc:
            raise errors.AppError(errors.WORKSPACE_BUSY, exc.safe_message, 409) from None
