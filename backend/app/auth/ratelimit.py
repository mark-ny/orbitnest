"""DB-backed failure tracking with exponential backoff lockout.

Only failures are stored. A key is locked when it has >= ``threshold`` failures inside ``window``;
the lock lasts ``min(base * 2**(failures - threshold), cap)`` seconds from the most recent
failure. Locking is keyed on the *attempted* username hash whether or not the account exists, so
lockout behaviour does not reveal which usernames are real. Known trade-off: an attacker can
temporarily lock a victim's username (bounded by ``cap``); see docs/THREAT_MODEL.md.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.ids import from_iso, to_iso
from app.db import Database


def key_hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Rule:
    scope: str
    threshold: int
    window: timedelta = timedelta(minutes=15)
    base_seconds: int = 30
    cap_seconds: int = 900


LOGIN_USER = Rule("login_user", threshold=5)
LOGIN_IP = Rule("login_ip", threshold=20)
SETUP_IP = Rule("setup_ip", threshold=5)
RESET_IP = Rule("reset_ip", threshold=10)


class RateLimiter:
    def __init__(self, db: Database) -> None:
        self.db = db

    def retry_after(self, rule: Rule, key: str, now: datetime) -> int:
        """Seconds until the key may try again; 0 when not locked."""
        hashed = key_hash(key)
        since = to_iso(now - rule.window)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(occurred_at) AS last FROM auth_failures "
                "WHERE scope = ? AND key_hash = ? AND occurred_at >= ?",
                (rule.scope, hashed, since),
            ).fetchone()
        count = row["n"]
        if count < rule.threshold or row["last"] is None:
            return 0
        lock = min(rule.base_seconds * 2 ** (count - rule.threshold), rule.cap_seconds)
        unlock_at = from_iso(row["last"]) + timedelta(seconds=lock)
        remaining = (unlock_at - now).total_seconds()
        return max(0, int(remaining) + (1 if remaining % 1 else 0))

    def record_failure(self, rule: Rule, key: str, now: datetime) -> None:
        hashed = key_hash(key)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO auth_failures (scope, key_hash, occurred_at) VALUES (?, ?, ?)",
                (rule.scope, hashed, to_iso(now)),
            )
            # Opportunistic purge keeps the table bounded.
            conn.execute(
                "DELETE FROM auth_failures WHERE occurred_at < ?",
                (to_iso(now - rule.window * 4),),
            )

    def clear(self, rule: Rule, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM auth_failures WHERE scope = ? AND key_hash = ?",
                (rule.scope, key_hash(key)),
            )
