"""File manager routes. Operate on the workspace's data directory on the host; they work while
the workspace is stopped. All paths are relative to the workspace root and validated in safefs."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import AsyncIterator
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from app.auth.deps import authenticate, request_context
from app.auth.policy import Perm
from app.core import errors
from app.core.errors import parse_json_body
from app.services.files import FileService, to_app_error
from app.storage.safefs import FsError

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


class _PathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=1024)


class _MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=1024)
    destination: str = Field(max_length=1024)


def _files(request: Request) -> FileService:
    svc: FileService = request.app.state.files
    return svc


def _wid(request: Request) -> str:
    return str(request.path_params["workspace_id"])


def _flag(request: Request, name: str) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return False
    if raw.lower() in ("1", "true"):
        return True
    if raw.lower() in ("0", "false"):
        return False
    raise errors.AppError(errors.VALIDATION_ERROR, f"{name} must be true or false.", 422)


def _path(request: Request) -> str:
    return request.query_params.get("path", "")


async def list_files(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_READ)
    path, entries, truncated = await run_in_threadpool(
        _files(request).list, auth.user, _wid(request), _path(request), request_context(request)
    )
    return JSONResponse(
        {
            "path": path,
            "truncated": truncated,
            "entries": [
                {"name": e.name, "type": e.kind, "size": e.size, "modified": e.modified} for e in entries
            ],
        }
    )


async def _iter_file(handle: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while True:
            chunk = await run_in_threadpool(handle.read, 64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


async def download(request: Request) -> Response:
    auth = authenticate(request, permission=Perm.WORKSPACE_READ)
    handle, size, name = await run_in_threadpool(
        _files(request).open_download, auth.user, _wid(request), _path(request), request_context(request)
    )
    ascii_name = _NAME_UNSAFE.sub("_", name)[:100] or "download"
    return StreamingResponse(
        _iter_file(handle),
        media_type="application/octet-stream",  # never let the browser render workload-controlled content
        headers={
            "content-length": str(size),
            "content-disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"
            ),
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


async def upload(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    declared = request.headers.get("content-length")
    try:
        declared_size = int(declared) if declared is not None else None
    except ValueError:
        raise errors.AppError(errors.VALIDATION_ERROR, "Invalid Content-Length.", 422) from None
    overwrite = _flag(request, "overwrite")
    ctx = request_context(request)
    svc = _files(request)
    ws, parts, writer = await run_in_threadpool(
        lambda: svc.begin_upload(
            auth.user,
            _wid(request),
            _path(request),
            overwrite=overwrite,
            declared_size=declared_size,
            ctx=ctx,
        )
    )
    try:
        async for chunk in request.stream():
            if chunk:
                try:
                    await run_in_threadpool(writer.write, chunk)
                except FsError as exc:
                    raise to_app_error(exc) from None
        size = await run_in_threadpool(svc.finish_upload, auth.user, ws, parts, writer, ctx)
    except BaseException:
        await run_in_threadpool(writer.abort)
        raise
    return JSONResponse({"path": "/" + "/".join(parts), "size": size}, status_code=201)


async def make_dir(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    data = await parse_json_body(request, _PathBody)
    path = await run_in_threadpool(
        _files(request).mkdir, auth.user, _wid(request), data.path, request_context(request)
    )
    return JSONResponse({"path": path}, status_code=201)


async def move(request: Request) -> JSONResponse:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    data = await parse_json_body(request, _MoveBody)
    path = await run_in_threadpool(
        _files(request).move,
        auth.user,
        _wid(request),
        data.source,
        data.destination,
        request_context(request),
    )
    return JSONResponse({"path": path})


async def delete(request: Request) -> Response:
    auth = authenticate(request, permission=Perm.WORKSPACE_WRITE)
    recursive = _flag(request, "recursive")
    await run_in_threadpool(
        lambda: _files(request).delete(
            auth.user, _wid(request), _path(request), recursive=recursive, ctx=request_context(request)
        )
    )
    return Response(status_code=204)


routes = [
    Route("/workspaces/{workspace_id}/files", list_files, methods=["GET"]),
    Route("/workspaces/{workspace_id}/files", delete, methods=["DELETE"]),
    Route("/workspaces/{workspace_id}/files/content", download, methods=["GET"]),
    Route("/workspaces/{workspace_id}/files/content", upload, methods=["PUT"]),
    Route("/workspaces/{workspace_id}/files/mkdir", make_dir, methods=["POST"]),
    Route("/workspaces/{workspace_id}/files/move", move, methods=["POST"]),
]
