"""File manager service: authorization, quota and audit around :mod:`app.storage.safefs`."""

from __future__ import annotations

from typing import TYPE_CHECKING, BinaryIO

from app.core import errors
from app.core.config import Settings
from app.services import audit
from app.services.audit import RequestContext
from app.services.workspace_store import Workspace
from app.services.workspaces import WorkspaceService
from app.storage import safefs
from app.storage.safefs import Entry, FsError, SafeFs, Writer, split_path

if TYPE_CHECKING:
    from app.auth.service import User

MIB = 1024 * 1024

_STATUS = {
    "not_found": (errors.NOT_FOUND, 404),
    "exists": (errors.CONFLICT, 409),
    "invalid_path": (errors.VALIDATION_ERROR, 422),
    "not_a_directory": (errors.CONFLICT, 409),
    "is_a_directory": (errors.CONFLICT, 409),
    "not_a_file": (errors.CONFLICT, 409),
    "not_empty": (errors.CONFLICT, 409),
    "too_large": (errors.PAYLOAD_TOO_LARGE, 413),
    "forbidden": (errors.FORBIDDEN, 403),
    "unsupported": (errors.NOT_SUPPORTED, 501),
}


def to_app_error(exc: FsError) -> errors.AppError:
    code, status = _STATUS.get(exc.code, (errors.INTERNAL_ERROR, 500))
    return errors.AppError(code, exc.message, status)


def display_path(parts: list[str]) -> str:
    return "/" + "/".join(parts)


class FileService:
    def __init__(self, workspaces: WorkspaceService, settings: Settings) -> None:
        self.workspaces = workspaces
        self.settings = settings

    def _open(
        self, actor: User, workspace_id: str, *, write: bool, ctx: RequestContext | None
    ) -> tuple[Workspace, SafeFs]:
        ws = self.workspaces.access(actor, workspace_id, write=write, ctx=ctx)
        if ws.status in ("deleting", "deleted") or ws.volume_state not in (None, "active"):
            raise errors.AppError(
                errors.WORKSPACE_DELETED, "This workspace's files are no longer available.", 409
            )
        try:
            return ws, SafeFs(self.workspaces.storage.volume_dir(ws.id))
        except FsError as exc:
            raise to_app_error(exc) from None

    def _audit(
        self, actor: User, action: str, ws: Workspace, ctx: RequestContext | None, **details: object
    ) -> None:
        audit.record(
            self.workspaces.db,
            action=f"file.{action}",
            outcome="success",
            actor_user_id=actor.id,
            target_type="workspace",
            target_id=ws.id,
            ctx=ctx,
            details={k: (v[:200] if isinstance(v, str) else v) for k, v in details.items()},
        )

    def list(
        self, actor: User, workspace_id: str, path: str, ctx: RequestContext | None = None
    ) -> tuple[str, list[Entry], bool]:
        _ws, fs = self._open(actor, workspace_id, write=False, ctx=ctx)
        try:
            parts = split_path(path)
            entries, truncated = fs.list_dir(parts)
        except FsError as exc:
            raise to_app_error(exc) from None
        return display_path(parts), entries, truncated

    def open_download(
        self, actor: User, workspace_id: str, path: str, ctx: RequestContext | None = None
    ) -> tuple[BinaryIO, int, str]:
        ws, fs = self._open(actor, workspace_id, write=False, ctx=ctx)
        try:
            parts = split_path(path)
            handle, size, _mtime = fs.open_read(parts)
        except FsError as exc:
            raise to_app_error(exc) from None
        self._audit(actor, "download", ws, ctx, path=display_path(parts), bytes=size)
        return handle, size, parts[-1]

    def begin_upload(
        self,
        actor: User,
        workspace_id: str,
        path: str,
        *,
        overwrite: bool,
        declared_size: int | None,
        ctx: RequestContext | None = None,
    ) -> tuple[Workspace, list[str], Writer]:
        ws, fs = self._open(actor, workspace_id, write=True, ctx=ctx)
        try:
            parts = split_path(path)
            if not parts:
                raise FsError("invalid_path", "Give the file's path.")
            quota = ws.storage_quota_mb * MIB
            used = self.workspaces.storage.usage_bytes(ws.id)
            allowed = min(self.settings.max_upload_mb * MIB, max(0, quota - used))
            if declared_size is not None and declared_size > allowed:
                raise FsError("too_large", "The file is larger than the allowed size or storage quota.")
            return ws, parts, fs.begin_write(parts, max_bytes=allowed, overwrite=overwrite)
        except FsError as exc:
            raise to_app_error(exc) from None

    def finish_upload(
        self, actor: User, ws: Workspace, parts: list[str], writer: Writer, ctx: RequestContext | None = None
    ) -> int:
        try:
            size = writer.commit()
        except FsError as exc:
            raise to_app_error(exc) from None
        self._audit(actor, "upload", ws, ctx, path=display_path(parts), bytes=size)
        return size

    def mkdir(self, actor: User, workspace_id: str, path: str, ctx: RequestContext | None = None) -> str:
        ws, fs = self._open(actor, workspace_id, write=True, ctx=ctx)
        try:
            parts = split_path(path)
            if not parts:
                raise FsError("exists", "That folder already exists.")
            fs.mkdir(parts)
        except FsError as exc:
            raise to_app_error(exc) from None
        self._audit(actor, "mkdir", ws, ctx, path=display_path(parts))
        return display_path(parts)

    def move(
        self, actor: User, workspace_id: str, src: str, dst: str, ctx: RequestContext | None = None
    ) -> str:
        ws, fs = self._open(actor, workspace_id, write=True, ctx=ctx)
        try:
            s, d = split_path(src), split_path(dst)
            fs.move(s, d)
        except FsError as exc:
            raise to_app_error(exc) from None
        self._audit(actor, "move", ws, ctx, source=display_path(s), destination=display_path(d))
        return display_path(d)

    def delete(
        self, actor: User, workspace_id: str, path: str, *, recursive: bool, ctx: RequestContext | None = None
    ) -> None:
        ws, fs = self._open(actor, workspace_id, write=True, ctx=ctx)
        try:
            parts = split_path(path)
            fs.delete(parts, recursive=recursive)
        except FsError as exc:
            raise to_app_error(exc) from None
        self._audit(actor, "delete", ws, ctx, path=display_path(parts), recursive=recursive)


__all__ = ["FileService", "safefs"]
