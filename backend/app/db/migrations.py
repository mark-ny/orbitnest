"""Versioned SQL migrations with checksum verification.

Migration files live in ``app/db/sql/NNNN_name.sql`` (contiguous from 0001). Applied
migrations are recorded with a SHA-256 checksum; editing an applied file is detected and refused.
Each migration runs in one transaction (SQLite DDL is transactional).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.db.database import Database

MIGRATIONS_DIR = Path(__file__).parent / "sql"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql\Z")

_BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT
"""


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class MigrationStatus:
    current: int
    latest: int
    pending: list[int]

    @property
    def up_to_date(self) -> bool:
        return not self.pending


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migration versions must be contiguous from 0001 (expected {expected:04d}, "
                f"found {migration.version:04d})"
            )
    return found


def _statements(script: str) -> list[str]:
    """Split a script into complete statements (trigger-safe via sqlite3.complete_statement)."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration ends with an incomplete statement")
    return statements


def _applied(conn: sqlite3.Connection) -> dict[int, str]:
    conn.execute(_BOOKKEEPING)
    rows = conn.execute("SELECT version, checksum FROM schema_migrations")
    return {row["version"]: row["checksum"] for row in rows}


def migration_status(db: Database, migrations: list[Migration] | None = None) -> MigrationStatus:
    available = discover() if migrations is None else migrations
    with db.connect() as conn:
        applied = _applied(conn)
    latest = available[-1].version if available else 0
    return MigrationStatus(
        current=max(applied, default=0),
        latest=latest,
        pending=[m.version for m in available if m.version not in applied],
    )


def migrate(db: Database, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied."""
    available = discover() if migrations is None else migrations
    applied_now: list[int] = []
    with db.connect() as conn:
        applied = _applied(conn)
        known = {m.version: m for m in available}
        for version, checksum in applied.items():
            migration = known.get(version)
            if migration is None:
                raise MigrationError(f"database has migration {version:04d} unknown to this build")
            if migration.checksum != checksum:
                raise MigrationError(f"migration {version:04d} was modified after being applied")
        for migration in available:
            if migration.version in applied:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _statements(migration.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.checksum),
                )
            except BaseException as exc:
                conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise MigrationError(f"migration {migration.version:04d} failed: {exc}") from exc
                raise
            conn.execute("COMMIT")
            applied_now.append(migration.version)
    return applied_now
