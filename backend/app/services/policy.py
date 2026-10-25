"""Administrator-configurable policy, stored in ``system_settings`` with validated ranges."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from app.core import errors
from app.db import Database
from app.services import settings_store

PREFIX = "policy."


@dataclass(frozen=True)
class Policy:
    max_workspaces_per_user: int = 5
    max_running_per_user: int = 2
    idle_mark_minutes: int = 15  # inactive this long => status "idle"
    idle_action: str = "stop"  # "stop" workspaces past their idle timeout, or "mark" only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_RANGES: dict[str, tuple[int, int]] = {
    "max_workspaces_per_user": (1, 100),
    "max_running_per_user": (1, 50),
    "idle_mark_minutes": (1, 24 * 60),
}
_IDLE_ACTIONS = ("stop", "mark")


def load(db: Database) -> Policy:
    defaults = Policy()
    values: dict[str, Any] = {}
    for f in fields(Policy):
        raw = settings_store.get_setting(db, PREFIX + f.name)
        if raw is None:
            continue
        try:
            values[f.name] = int(raw) if f.name in _RANGES else raw
        except ValueError:
            continue  # ignore a corrupt row; fall back to the default
    merged = Policy(**{**asdict(defaults), **values})
    return merged


def update(db: Database, changes: dict[str, Any]) -> Policy:
    """Validate and persist. Unknown keys and out-of-range values are rejected."""
    problems: list[dict[str, Any]] = []
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key in _RANGES:
            low, high = _RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                problems.append(
                    {
                        "field": key,
                        "reason": f"must be an integer from {low} to {high}",
                        "type": "value_error",
                    }
                )
            else:
                clean[key] = value
        elif key == "idle_action":
            if value not in _IDLE_ACTIONS:
                problems.append(
                    {
                        "field": key,
                        "reason": f"must be one of {', '.join(_IDLE_ACTIONS)}",
                        "type": "value_error",
                    }
                )
            else:
                clean[key] = value
        else:
            problems.append({"field": key, "reason": "unknown setting", "type": "value_error"})
    if problems:
        raise errors.AppError(errors.VALIDATION_ERROR, "Request body is invalid.", 422, problems)
    with db.transaction() as conn:
        for key, value in clean.items():
            settings_store.set_setting(db, PREFIX + key, str(value), conn=conn)
    return load(db)
