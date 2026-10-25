"""Per-workspace data directories under ``<data_dir>/workspaces/<workspace id>/data``.

Only ULID-shaped ids ever become path components, so no user-controlled string reaches the
filesystem layer. Deletion uses ``shutil.rmtree`` which removes symlinks without following them.

Quota note: ``usage_bytes`` is for reporting and for the file manager's upload checks (Phase 6).
Writes made by the workload itself are NOT capped at the filesystem level (that needs XFS project
quotas or a per-workspace filesystem); this limitation is documented in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path

ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
CONTAINER_UID = 1000


class StorageError(Exception):
    pass


class StorageManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)

    def _base(self, workspace_id: str) -> Path:
        if not ID_RE.match(workspace_id):
            raise StorageError("invalid workspace id")
        return self.root / workspace_id

    def volume_dir(self, workspace_id: str) -> Path:
        return self._base(workspace_id) / "data"

    @staticmethod
    def relative_path(workspace_id: str) -> str:
        return f"{workspace_id}/data"

    def exists(self, workspace_id: str) -> bool:
        return self.volume_dir(workspace_id).is_dir()

    def create(self, workspace_id: str) -> Path:
        path = self.volume_dir(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.chmod(0o700)
        # When the control plane runs as root, containers run as uid 1000 and must own the folder.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            with contextlib.suppress(OSError):
                os.chown(path, CONTAINER_UID, CONTAINER_UID)
        return path

    def wipe_contents(self, workspace_id: str) -> None:
        """Delete everything inside the data directory but keep the directory itself."""
        path = self.volume_dir(workspace_id)
        if not path.is_dir():
            return
        for child in path.iterdir():
            self._remove(child)

    def purge(self, workspace_id: str) -> None:
        base = self._base(workspace_id)
        if base.is_symlink():
            base.unlink()
        elif base.exists():
            shutil.rmtree(base)

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)

    def usage_bytes(self, workspace_id: str) -> int:
        path = self.volume_dir(workspace_id)
        total = 0
        if not path.is_dir():
            return 0
        for dirpath, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(dirpath, name)).st_size
        return total

    def known_ids(self) -> set[str]:
        return {p.name for p in self.root.iterdir() if ID_RE.match(p.name)} if self.root.is_dir() else set()
