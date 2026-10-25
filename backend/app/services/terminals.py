"""Terminal sessions: authorization, limits, lifecycle and audit around runtime shells.

A terminal is an interactive shell *inside* the workspace (``docker exec`` for containers). For
the local (Termux) runtime there is no isolation, so a terminal is a shell as the control plane's
own OS user and is therefore restricted to administrators.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from app.core import errors
from app.core.config import Settings
from app.runtimes.base import RuntimeFailure
from app.runtimes.pty_shell import ShellSession
from app.services import audit, lifecycle
from app.services.audit import RequestContext
from app.services.workspaces import WorkspaceService

if TYPE_CHECKING:
    from app.auth.service import User


class TerminalSession:
    def __init__(
        self, service: TerminalService, workspace_id: str, user_id: str, shell: ShellSession
    ) -> None:
        self.service = service
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.shell = shell
        self.opened = time.monotonic()
        self.bytes_in = 0
        self.bytes_out = 0
        self._closed = False
        self._lock = threading.Lock()

    def close(self, reason: str = "closed") -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.shell.close()
        self.service._release(self, reason)


class TerminalService:
    def __init__(self, workspaces: WorkspaceService, settings: Settings) -> None:
        self.workspaces = workspaces
        self.settings = settings
        self._lock = threading.Lock()
        self._sessions: set[TerminalSession] = set()
        self._pending: dict[str, int] = {}  # reserved slots keyed "ws:<id>" / "user:<id>"
        workspaces.on_inactive.append(self.close_workspace)

    def active(self, workspace_id: str | None = None) -> int:
        with self._lock:
            return sum(1 for s in self._sessions if workspace_id in (None, s.workspace_id))

    def open(
        self, actor: User, workspace_id: str, cols: int, rows: int, ctx: RequestContext | None = None
    ) -> TerminalSession:
        ws = self.workspaces.access(actor, workspace_id, write=True, ctx=ctx)
        if ws.status not in lifecycle.LIVE:
            raise errors.AppError(
                errors.WORKSPACE_BUSY,
                f"The workspace is {ws.status}. Start it before opening a terminal.",
                409,
            )
        if ws.runtime == "local" and actor.role != "admin":
            audit.record(
                self.workspaces.db,
                action="terminal.open",
                outcome="denied",
                actor_user_id=actor.id,
                target_type="workspace",
                target_id=ws.id,
                ctx=ctx,
                details={"why": "local runtime has no isolation; administrators only"},
            )
            raise errors.AppError(
                errors.FORBIDDEN,
                "Terminals on this host run without isolation and are limited to administrators.",
                403,
            )
        adapter = self.workspaces.adapters().get(ws.runtime)
        inst = self.workspaces.store.current_instance(ws.id)
        if adapter is None or inst is None:
            raise errors.AppError(errors.WORKSPACE_BUSY, "The workspace has no running instance.", 409)

        with self._lock:  # reserve before spawning so concurrent opens cannot exceed the limits
            per_ws = sum(1 for s in self._sessions if s.workspace_id == ws.id) + self._pending.get(
                f"ws:{ws.id}", 0
            )
            per_user = sum(1 for s in self._sessions if s.user_id == actor.id) + self._pending.get(
                f"user:{actor.id}", 0
            )
            if per_ws >= self.settings.max_terminals_per_workspace:
                raise errors.AppError(
                    errors.SESSION_LIMIT,
                    "This workspace already has the maximum number of terminals open.",
                    409,
                )
            if per_user >= self.settings.max_terminals_per_user:
                raise errors.AppError(
                    errors.SESSION_LIMIT, "You already have the maximum number of terminals open.", 409
                )
            for key in (f"ws:{ws.id}", f"user:{actor.id}"):
                self._pending[key] = self._pending.get(key, 0) + 1
        try:
            shell = adapter.open_shell(inst.resource_id, cols, rows)
        except RuntimeFailure as exc:
            raise errors.AppError(errors.WORKSPACE_BUSY, exc.safe_message, 409) from None
        finally:
            with self._lock:
                for key in (f"ws:{ws.id}", f"user:{actor.id}"):
                    self._pending[key] -= 1
        session = TerminalSession(self, ws.id, actor.id, shell)
        with self._lock:
            self._sessions.add(session)
        audit.record(
            self.workspaces.db,
            action="terminal.open",
            outcome="success",
            actor_user_id=actor.id,
            target_type="workspace",
            target_id=ws.id,
            ctx=ctx,
        )
        return session

    def _release(self, session: TerminalSession, reason: str) -> None:
        with self._lock:
            self._sessions.discard(session)
        audit.record(
            self.workspaces.db,
            action="terminal.close",
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
        for session in victims:
            session.close("workspace stopped")

    def close_all(self) -> None:
        with self._lock:
            victims = list(self._sessions)
        for session in victims:
            session.close("server shutdown")
