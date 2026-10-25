"""Key/value system settings (table ``system_settings``). Values are plain text, never secrets."""

from __future__ import annotations

import sqlite3

from app.db import Database


def get_setting(db: Database, key: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_setting(db: Database, key: str, value: str, *, conn: sqlite3.Connection | None = None) -> None:
    sql = (
        "INSERT INTO system_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    )
    if conn is not None:
        conn.execute(sql, (key, value))
        return
    with db.transaction() as c:
        c.execute(sql, (key, value))
