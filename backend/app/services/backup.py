"""Backup and restore of the control plane's state.

A backup is a ``.tar.gz`` holding a consistent SQLite snapshot (taken with SQLite's online backup
API, so it is safe while the server runs) plus, optionally, the managed ``secret.key`` and the
workspace data directories. Archives contain credentials-equivalent material (password hashes,
session records, the signing key): they are written mode 0600 and must be stored like secrets.

Restore refuses to overwrite an existing database and validates every archive member, so a
hostile or corrupt archive cannot write outside the data directory.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings

DB_MEMBER = "orbitnest.sqlite3"
SECRET_MEMBER = "secret.key"  # noqa: S105 - file name, not a secret
WORKSPACES_PREFIX = "workspaces/"
ALLOWED_TOP = {DB_MEMBER, SECRET_MEMBER}


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    includes_secret: bool
    includes_workspaces: bool


def create_backup(
    settings: Settings, dest_dir: Path, *, include_workspaces: bool = False, now: datetime | None = None
) -> BackupResult:
    if not settings.db_path.exists():
        raise BackupError("no database found; nothing to back up")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    out = dest_dir / f"orbitnest-backup-{stamp}.tar.gz"
    if out.exists():
        raise BackupError(f"{out} already exists")
    secret = settings.data_dir / SECRET_MEMBER
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / DB_MEMBER
        src = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                tar.add(snap, arcname=DB_MEMBER)
                if secret.is_file():
                    tar.add(secret, arcname=SECRET_MEMBER)
                if include_workspaces and settings.workspaces_root.is_dir():
                    tar.add(settings.workspaces_root, arcname=WORKSPACES_PREFIX.rstrip("/"))
        except BaseException:
            with contextlib.suppress(OSError):
                out.unlink()
            raise
    return BackupResult(out, secret.is_file(), include_workspaces and settings.workspaces_root.is_dir())


def _check_member(m: tarfile.TarInfo) -> None:
    name = m.name
    parts = Path(name).parts
    if Path(name).is_absolute() or ".." in parts or not parts:
        raise BackupError(f"unsafe path in archive: {name!r}")
    if not (m.isfile() or m.isdir()):
        raise BackupError(f"archive member {name!r} is not a regular file or directory")
    top = parts[0]
    if top not in ALLOWED_TOP and top != WORKSPACES_PREFIX.rstrip("/"):
        raise BackupError(f"unexpected member in archive: {name!r}")
    if top in ALLOWED_TOP and (len(parts) != 1 or not m.isfile()):
        raise BackupError(f"unexpected member in archive: {name!r}")


def restore_backup(settings: Settings, archive: Path) -> list[str]:
    """Restore into the (empty) data directory. Returns the restored top-level names."""
    if settings.db_path.exists():
        raise BackupError(
            f"{settings.db_path} already exists; refusing to overwrite. Stop the service and move the "
            "existing data directory aside first."
        )
    if not archive.is_file():
        raise BackupError(f"{archive} is not a file")
    restored: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                _check_member(m)
            if not any(m.name == DB_MEMBER for m in members):
                raise BackupError("archive has no database snapshot")
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                settings.data_dir.chmod(0o700)
            for m in members:
                # Ownership and special bits from the archive are never applied.
                m.uid = m.gid = 0
                m.uname = m.gname = ""
                m.mode &= 0o755 if m.isdir() else 0o644
                target_top = Path(m.name).parts[0]
                if target_top == DB_MEMBER:
                    # The DB may live elsewhere when ORBITNEST_DATABASE_PATH is set.
                    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(m) as src:  # type: ignore[union-attr]
                        fd = os.open(settings.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as dst:
                            dst.write(src.read())
                else:
                    tar.extract(m, settings.data_dir, filter="data")
                    if target_top == SECRET_MEMBER:
                        with contextlib.suppress(OSError):
                            (settings.data_dir / SECRET_MEMBER).chmod(0o600)
                restored.add(target_top)
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"could not read archive: {exc}") from None
    return sorted(restored)
