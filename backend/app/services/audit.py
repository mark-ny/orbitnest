"""Audit log. Details are redacted and size-capped before storage; never put secrets in them."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.ids import new_id
from app.core.logging import redact, request_id_var
from app.db import Database

MAX_DETAILS_BYTES = 4096
MAX_PAGE = 200
OUTCOMES = ("success", "failure", "denied")


@dataclass(frozen=True)
class RequestContext:
    """Per-request facts attached to audit events."""

    ip: str | None = None
    request_id: str | None = None
    user_agent: str | None = None


def record(
    db: Database,
    *,
    action: str,
    outcome: str,
    actor_user_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    ctx: RequestContext | None = None,
    details: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Insert an audit event. Pass ``conn`` to commit it atomically with the audited change."""
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid audit outcome: {outcome}")
    event_id = new_id()
    payload = json.dumps(redact(details or {}), default=str, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_DETAILS_BYTES:
        payload = json.dumps({"truncated": True})
    request_id = (ctx.request_id if ctx else None) or request_id_var.get()
    params = (
        event_id,
        actor_user_id,
        action,
        target_type,
        target_id,
        outcome,
        None if request_id == "-" else request_id,
        ctx.ip if ctx else None,
        payload,
    )
    sql = (
        "INSERT INTO audit_events "
        "(id, actor_user_id, action, target_type, target_id, outcome, request_id, ip, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as c:
            c.execute(sql, params)
    return event_id


def _encode_cursor(occurred_at: str, event_id: str) -> str:
    return base64.urlsafe_b64encode(f"{occurred_at}|{event_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        occurred_at, event_id = raw.split("|", 1)
        return occurred_at, event_id
    except (ValueError, UnicodeDecodeError):
        return None


def list_events(
    db: Database,
    *,
    limit: int = 50,
    cursor: str | None = None,
    action_prefix: str | None = None,
    outcome: str | None = None,
    actor_user_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Newest-first keyset pagination. Returns (events, next_cursor)."""
    limit = max(1, min(limit, MAX_PAGE))
    where, params = [], []  # type: list[str], list[Any]
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded is None:
            raise ValueError("invalid cursor")
        where.append("(occurred_at < ? OR (occurred_at = ? AND id < ?))")
        params += [decoded[0], decoded[0], decoded[1]]
    if action_prefix:
        escaped = action_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("action LIKE ? ESCAPE '\\'")
        params.append(escaped + "%")
    if outcome:
        where.append("outcome = ?")
        params.append(outcome)
    if actor_user_id:
        where.append("actor_user_id = ?")
        params.append(actor_user_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # `clause` is assembled only from fixed fragments above; all values are bound parameters.
    sql = (
        "SELECT id, occurred_at, actor_user_id, action, target_type, target_id, outcome, "  # noqa: S608
        "request_id, ip, details FROM audit_events "
        f"{clause} ORDER BY occurred_at DESC, id DESC LIMIT ?"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, [*params, limit + 1]).fetchall()
    page = rows[:limit]
    events = [{**dict(r), "details": json.loads(r["details"])} for r in page]
    next_cursor = _encode_cursor(page[-1]["occurred_at"], page[-1]["id"]) if len(rows) > limit else None
    return events, next_cursor
