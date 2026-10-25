"""Role-based access control policy. Pure functions; enforced in services/handlers, not the UI."""

from __future__ import annotations

from typing import Literal

Role = Literal["admin", "user", "read_only"]
ROLES: tuple[Role, ...] = ("admin", "user", "read_only")


class Perm:
    SYSTEM_READ = "system:read"
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_WRITE = "workspace:write"
    USERS_MANAGE = "users:manage"
    AUDIT_READ = "audit:read"
    SETTINGS_MANAGE = "settings:manage"
    TEMPLATES_MANAGE = "templates:manage"


_USER = frozenset({Perm.SYSTEM_READ, Perm.WORKSPACE_READ, Perm.WORKSPACE_WRITE})
_READ_ONLY = frozenset({Perm.SYSTEM_READ, Perm.WORKSPACE_READ})
_ADMIN = _USER | {Perm.USERS_MANAGE, Perm.AUDIT_READ, Perm.SETTINGS_MANAGE, Perm.TEMPLATES_MANAGE}

PERMISSIONS: dict[str, frozenset[str]] = {"admin": _ADMIN, "user": _USER, "read_only": _READ_ONLY}


def has_permission(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())


def can_access_workspace(*, user_id: str, role: str, owner_id: str, write: bool) -> bool:
    """Owners act within their role's rights; admins may access any workspace.

    Used by the workspace service (Phase 3) so ownership is enforced in the backend (BOLA/IDOR).
    """
    needed = Perm.WORKSPACE_WRITE if write else Perm.WORKSPACE_READ
    if not has_permission(role, needed):
        return False
    return role == "admin" or user_id == owner_id
