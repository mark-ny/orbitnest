from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str
    is_active: bool
    created_at: str
    last_login_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    def to_public(self) -> dict[str, Any]:
        """Never includes password hashes."""
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }
