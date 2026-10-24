"""SQLite access. This module is the only SQL boundary (ADR-001).

All statements must be parameterized by callers; there is no string-formatting helper here on
purpose. Requires SQLite >= 3.37 for STRICT tables.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIN_SQLITE = (3, 37, 0)


class Database:
    def __init__(self, path: Path) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(
                f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ is required (found {sqlite3.sqlite_version})"
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        first_create = not self.path.exists()
        with self.connect():
            pass
        if first_create:
            with contextlib.suppress(OSError):
                self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
