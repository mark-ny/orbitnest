"""Symlink-safe file operations inside one workspace data directory.

The workload owns the directory and may be hostile, so path strings are never handed to the OS.
Every component is opened relative to the previous directory descriptor with ``O_NOFOLLOW`` (the
``openat`` pattern), which defeats ``..``, absolute paths and symlinks planted or swapped at any
time, including between a check and its use. Symlinks are listed but never followed.

Requires ``dir_fd`` support (Linux, Android/Termux, macOS). On platforms without it the file
manager reports itself unsupported instead of falling back to weaker path checks.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_DEPTH = 32
MAX_PATH_CHARS = 1024
MAX_NAME_BYTES = 255
MAX_ENTRIES = 2000
MAX_DELETE_DEPTH = 64
CHUNK = 64 * 1024
CONTAINER_UID = 1000

_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)
_TMP_PREFIX = ".orbitnest-upload-"


class FsError(Exception):
    """``code`` is one of: unsupported, invalid_path, not_found, exists, not_a_directory,
    is_a_directory, not_a_file, not_empty, too_large, forbidden."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def supported() -> bool:
    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )


def split_path(raw: str) -> list[str]:
    """Validate a client path ('' or '/' is the root) and return its components."""
    if not isinstance(raw, str) or len(raw) > MAX_PATH_CHARS or "\x00" in raw or "\\" in raw:
        raise FsError("invalid_path", "Invalid path.")
    trimmed = raw.strip("/")
    if not trimmed:
        return []
    parts = trimmed.split("/")
    if len(parts) > MAX_DEPTH:
        raise FsError("invalid_path", "Path is too deep.")
    for part in parts:
        validate_name(part)
    return parts


def validate_name(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
        or name.startswith(_TMP_PREFIX)
    ):
        raise FsError("invalid_path", "Invalid file name.")
    try:
        if len(name.encode("utf-8")) > MAX_NAME_BYTES:
            raise FsError("invalid_path", "File name is too long.")
    except UnicodeEncodeError:
        raise FsError("invalid_path", "Invalid file name.") from None


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # file | dir | symlink | other
    size: int
    modified: float


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _chown_if_root(fd_or_none: int | None, name: str | None = None, dir_fd: int | None = None) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        with contextlib.suppress(OSError):
            if fd_or_none is not None:
                os.fchown(fd_or_none, CONTAINER_UID, CONTAINER_UID)
            elif name is not None:
                os.chown(name, CONTAINER_UID, CONTAINER_UID, dir_fd=dir_fd, follow_symlinks=False)


class Writer:
    """An in-progress upload. Always finish with ``commit`` or ``abort`` (abort is idempotent)."""

    def __init__(self, pfd: int, tmp: str, name: str, out: BinaryIO, max_bytes: int, overwrite: bool) -> None:
        self._pfd, self._tmp, self._name, self._out = pfd, tmp, name, out
        self._max, self._overwrite = max_bytes, overwrite
        self.size = 0
        self._done = False

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self._max:
            raise FsError("too_large", "The file is larger than the allowed size or storage quota.")
        self._out.write(chunk)

    def commit(self) -> int:
        try:
            self._out.close()
            if self._overwrite:
                os.rename(self._tmp, self._name, src_dir_fd=self._pfd, dst_dir_fd=self._pfd)
            else:
                try:
                    os.link(
                        self._tmp,
                        self._name,
                        src_dir_fd=self._pfd,
                        dst_dir_fd=self._pfd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise FsError("exists", "A file or folder with that name already exists.") from None
                os.unlink(self._tmp, dir_fd=self._pfd)
        except BaseException:
            self.abort()
            raise
        self._done = True
        os.close(self._pfd)
        return self.size

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        with contextlib.suppress(OSError):
            self._out.close()
        with contextlib.suppress(OSError):
            os.unlink(self._tmp, dir_fd=self._pfd)
        with contextlib.suppress(OSError):
            os.close(self._pfd)


class SafeFs:
    def __init__(self, root: Path) -> None:
        if not supported():
            raise FsError("unsupported", "The file manager needs a POSIX host (Linux, Termux or WSL 2).")
        self.root = str(root)

    # -- descriptors ---------------------------------------------------------------------
    @contextlib.contextmanager
    def _dir(self, parts: Iterable[str]) -> Iterator[int]:
        try:
            fd = os.open(self.root, _DIR_FLAGS)
        except OSError as exc:
            raise FsError("not_found", "This workspace has no data directory yet.") from exc
        try:
            for part in parts:
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    raise FsError("not_found", "Not found.") from None
                except OSError as exc:  # ENOTDIR, or ELOOP/EMLINK for a symlink under O_NOFOLLOW
                    if exc.errno in (errno.ENOTDIR, errno.ELOOP, errno.EMLINK):
                        raise FsError("not_a_directory", "Not a directory.") from None
                    raise FsError("forbidden", "Cannot open that path.") from None
                os.close(fd)
                fd = nxt
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _lstat(dfd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    # -- operations ----------------------------------------------------------------------
    def list_dir(self, parts: list[str]) -> tuple[list[Entry], bool]:
        entries: list[Entry] = []
        truncated = False
        with self._dir(parts) as fd, os.scandir(fd) as it:
            for e in it:
                if e.name.startswith(_TMP_PREFIX):
                    continue
                try:
                    e.name.encode("utf-8")
                    st = e.stat(follow_symlinks=False)
                except (UnicodeEncodeError, OSError):
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                kind = _kind(st.st_mode)
                entries.append(Entry(e.name, kind, st.st_size if kind == "file" else 0, st.st_mtime))
        entries.sort(key=lambda x: (x.kind != "dir", x.name.lower(), x.name))
        return entries, truncated

    def stat(self, parts: list[str]) -> Entry:
        if not parts:
            return Entry("", "dir", 0, 0.0)
        with self._dir(parts[:-1]) as fd:
            st = self._lstat(fd, parts[-1])
        if st is None:
            raise FsError("not_found", "Not found.")
        kind = _kind(st.st_mode)
        return Entry(parts[-1], kind, st.st_size if kind == "file" else 0, st.st_mtime)

    def open_read(self, parts: list[str]) -> tuple[BinaryIO, int, float]:
        """Open a regular file for reading. Symlinks, FIFOs and devices are refused."""
        if not parts:
            raise FsError("is_a_directory", "That is a directory.")
        with self._dir(parts[:-1]) as pfd:
            try:
                fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=pfd
                )
            except FileNotFoundError:
                raise FsError("not_found", "Not found.") from None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise FsError("not_a_file", "Symbolic links cannot be downloaded.") from None
                raise FsError("forbidden", "Cannot open that file.") from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise FsError(
                "is_a_directory" if stat.S_ISDIR(st.st_mode) else "not_a_file", "Not a regular file."
            )
        return os.fdopen(fd, "rb"), st.st_size, st.st_mtime

    def begin_write(self, parts: list[str], *, max_bytes: int, overwrite: bool) -> Writer:
        """Start an atomic upload: data goes to a hidden temp file and only ``commit`` makes it visible."""
        if not parts:
            raise FsError("is_a_directory", "That is a directory.")
        name = parts[-1]
        with self._dir(parts[:-1]) as dirfd:
            pfd = os.dup(dirfd)  # keep the directory open beyond this block
        try:
            existing = self._lstat(pfd, name)
            if existing is not None and (stat.S_ISDIR(existing.st_mode) or not overwrite):
                code = "is_a_directory" if stat.S_ISDIR(existing.st_mode) else "exists"
                raise FsError(code, "A file or folder with that name already exists.")
            tmp = f"{_TMP_PREFIX}{secrets.token_hex(8)}"
            fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=pfd
            )
        except BaseException:
            os.close(pfd)
            raise
        out = os.fdopen(fd, "wb")
        _chown_if_root(out.fileno())
        return Writer(pfd, tmp, name, out, max_bytes, overwrite)

    def write_file(
        self, parts: list[str], chunks: Iterable[bytes], *, max_bytes: int, overwrite: bool
    ) -> int:
        writer = self.begin_write(parts, max_bytes=max_bytes, overwrite=overwrite)
        try:
            for chunk in chunks:
                writer.write(chunk)
            return writer.commit()
        except BaseException:
            writer.abort()
            raise

    def mkdir(self, parts: list[str]) -> None:
        if not parts:
            raise FsError("exists", "That folder already exists.")
        with self._dir(parts[:-1]) as pfd:
            try:
                os.mkdir(parts[-1], 0o755, dir_fd=pfd)
            except FileExistsError:
                raise FsError("exists", "A file or folder with that name already exists.") from None
            _chown_if_root(None, parts[-1], pfd)

    def move(self, src: list[str], dst: list[str]) -> None:
        if not src or not dst:
            raise FsError("invalid_path", "Cannot move the workspace root.")
        if dst[: len(src)] == src:
            raise FsError("invalid_path", "Cannot move a folder into itself.")
        with self._dir(src[:-1]) as sfd, self._dir(dst[:-1]) as dfd:
            if self._lstat(sfd, src[-1]) is None:
                raise FsError("not_found", "Not found.")
            if self._lstat(dfd, dst[-1]) is not None:
                raise FsError("exists", "A file or folder with that name already exists.")
            os.rename(src[-1], dst[-1], src_dir_fd=sfd, dst_dir_fd=dfd)

    def delete(self, parts: list[str], *, recursive: bool) -> None:
        if not parts:
            raise FsError("forbidden", "The workspace root cannot be deleted.")
        with self._dir(parts[:-1]) as pfd:
            st = self._lstat(pfd, parts[-1])
            if st is None:
                raise FsError("not_found", "Not found.")
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(parts[-1], dir_fd=pfd)
            elif recursive:
                self._rmtree(pfd, parts[-1], 0)
            else:
                try:
                    os.rmdir(parts[-1], dir_fd=pfd)
                except OSError as exc:
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise FsError("not_empty", "The folder is not empty.") from None
                    raise

    def _rmtree(self, pfd: int, name: str, depth: int) -> None:
        if depth > MAX_DELETE_DEPTH:
            raise FsError("invalid_path", "Folder nesting is too deep to delete.")
        fd = os.open(name, _DIR_FLAGS, dir_fd=pfd)
        try:
            with os.scandir(fd) as it:
                children = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
            for child, is_dir in children:
                if is_dir:
                    self._rmtree(fd, child, depth + 1)
                else:
                    os.unlink(child, dir_fd=fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=pfd)
