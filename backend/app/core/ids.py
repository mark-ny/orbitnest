"""Opaque, time-sortable identifiers (ULID, Crockford base32) and UTC timestamp helpers."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(now_ms: int | None = None) -> str:
    """26-char ULID: 48-bit millisecond timestamp + 80 random bits."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Format matching SQLite's strftime('%Y-%m-%dT%H:%M:%fZ'): sortable, millisecond precision."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
