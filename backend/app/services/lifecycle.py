"""Workspace lifecycle state machine (pure; no I/O)."""

from __future__ import annotations

from typing import Final, Literal

Status = Literal[
    "provisioning",
    "starting",
    "running",
    "idle",
    "stopping",
    "stopped",
    "failed",
    "deleting",
    "deleted",
]

STATES: Final[tuple[str, ...]] = (
    "provisioning",
    "starting",
    "running",
    "idle",
    "stopping",
    "stopped",
    "failed",
    "deleting",
    "deleted",
)

# A background operation is (or was) in flight. After a crash these are "abandoned" and reconciled.
TRANSITIONAL: Final[frozenset[str]] = frozenset({"provisioning", "starting", "stopping", "deleting"})
# Statuses that hold runtime resources and count toward a user's concurrent-session limit.
ACTIVE: Final[frozenset[str]] = frozenset({"provisioning", "starting", "running", "idle"})
# The workspace is serving (or can serve) its user.
LIVE: Final[frozenset[str]] = frozenset({"running", "idle"})

ALLOWED: Final[dict[str, frozenset[str]]] = {
    "provisioning": frozenset({"starting", "stopped", "failed", "deleting"}),
    "starting": frozenset({"running", "failed", "stopping", "deleting"}),
    "running": frozenset({"idle", "stopping", "failed", "deleting"}),
    "idle": frozenset({"running", "stopping", "failed", "deleting"}),
    "stopping": frozenset({"stopped", "failed", "deleting"}),
    # stopped -> provisioning is the "reset" path (re-create the runtime instance).
    "stopped": frozenset({"starting", "provisioning", "deleting", "failed"}),
    "failed": frozenset({"starting", "provisioning", "stopping", "stopped", "deleting"}),
    "deleting": frozenset({"deleted", "failed"}),
    "deleted": frozenset(),
}


class InvalidTransition(ValueError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"cannot move a workspace from {current!r} to {target!r}")
        self.current = current
        self.target = target


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, frozenset())


def check_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


def sources_for(target: str) -> frozenset[str]:
    """All statuses from which ``target`` is a legal next status."""
    return frozenset(s for s, nxt in ALLOWED.items() if target in nxt)
