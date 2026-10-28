"""Database access for workspaces. All state changes go through :meth:`WorkspaceStore.transition`,
an atomic compare-and-set inside a ``BEGIN IMMEDIATE`` transaction, so concurrent operations
(two requests, a request and the sweeper, a job and a delete) can never both win."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.core.ids import new_id, to_iso, utcnow
from app.db import Database
from app.services import lifecycle

_SETTABLE = frozenset({"started_at", "last_activity_at", "last_health_at", "last_health_ok", "deleted_at"})


class QuotaExceeded(Exception):
    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(kind)
        self.kind = kind  # "workspaces" | "running"
        self.limit = limit


@dataclass(frozen=True)
class Workspace:
    id: str
    owner_id: str
    name: str
    template_id: str
    runtime: str
    status: str
    status_reason: str | None
    persistence: str
    destroy_on_stop: bool
    resource_profile: str
    cpu_millis: int
    memory_mb: int
    pids_limit: int
    storage_quota_mb: int
    idle_timeout_minutes: int | None
    network_policy: str
    labels: dict[str, str]
    extra_caps: tuple[str, ...]
    egress: str
    created_at: str
    updated_at: str
    started_at: str | None
    last_activity_at: str | None
    last_health_at: str | None
    last_health_ok: bool | None
    deleted_at: str | None
    volume_state: str | None = None
    volume_mount: str | None = None
    owner_username: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Workspace:
        keys = row.keys()
        health = row["last_health_ok"]
        return cls(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            template_id=row["template_id"],
            runtime=row["runtime"],
            status=row["status"],
            status_reason=row["status_reason"],
            persistence=row["persistence"],
            destroy_on_stop=bool(row["destroy_on_stop"]),
            resource_profile=row["resource_profile"],
            cpu_millis=row["cpu_millis"],
            memory_mb=row["memory_mb"],
            pids_limit=row["pids_limit"],
            storage_quota_mb=row["storage_quota_mb"],
            idle_timeout_minutes=row["idle_timeout_minutes"],
            network_policy=row["network_policy"],
            labels=json.loads(row["labels"] or "{}"),
            extra_caps=tuple(json.loads(row["extra_caps"] or "[]")) if "extra_caps" in keys else (),
            egress=row["egress"] if "egress" in keys else "direct",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            last_activity_at=row["last_activity_at"],
            last_health_at=row["last_health_at"],
            last_health_ok=None if health is None else bool(health),
            deleted_at=row["deleted_at"],
            volume_state=row["volume_state"] if "volume_state" in keys else None,
            volume_mount=row["volume_mount"] if "volume_mount" in keys else None,
            owner_username=row["owner_username"] if "owner_username" in keys else None,
        )


@dataclass(frozen=True)
class Instance:
    id: str
    workspace_id: str
    runtime: str
    resource_id: str
    image: str | None
    created_at: str


_SELECT = (
    "SELECT w.*, v.state AS volume_state, v.mount_path AS volume_mount, u.username AS owner_username "
    "FROM workspaces w LEFT JOIN storage_volumes v ON v.workspace_id = w.id "
    "LEFT JOIN users u ON u.id = w.owner_id "
)


class WorkspaceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- reads ---------------------------------------------------------------------------
    def get(self, workspace_id: str, conn: sqlite3.Connection | None = None) -> Workspace | None:
        def q(c: sqlite3.Connection) -> Workspace | None:
            row = c.execute(_SELECT + "WHERE w.id = ?", (workspace_id,)).fetchone()
            return Workspace.from_row(row) if row else None

        if conn is not None:
            return q(conn)
        with self.db.connect() as c:
            return q(c)

    def by_idempotency_key(self, owner_id: str, key: str) -> Workspace | None:
        with self.db.connect() as c:
            row = c.execute(
                _SELECT + "WHERE w.owner_id = ? AND w.idempotency_key = ?", (owner_id, key)
            ).fetchone()
        return Workspace.from_row(row) if row else None

    def list(
        self,
        *,
        owner_id: str | None,
        status: str | None = None,
        template_id: str | None = None,
        query: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Workspace], int]:
        where: list[str] = []
        params: list[Any] = []
        if owner_id is not None:
            where.append("w.owner_id = ?")
            params.append(owner_id)
        if status:
            where.append("w.status = ?")
            params.append(status)
        elif not include_deleted:
            where.append("w.status != 'deleted'")
        if template_id:
            where.append("w.template_id = ?")
            params.append(template_id)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("w.name LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
        limit = max(1, min(limit, 200))
        with self.db.connect() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM workspaces w " + clause,  # noqa: S608 - fixed fragments only
                params,
            ).fetchone()[0]
            rows = c.execute(
                _SELECT + clause + "ORDER BY w.created_at DESC, w.id DESC LIMIT ? OFFSET ?",
                [*params, limit, max(0, offset)],
            ).fetchall()
        return [Workspace.from_row(r) for r in rows], int(total)

    def with_status(self, statuses: Iterable[str]) -> list[Workspace]:
        wanted = list(statuses)
        marks = ",".join("?" for _ in wanted)
        with self.db.connect() as c:
            rows = c.execute(_SELECT + f"WHERE w.status IN ({marks})", wanted).fetchall()
        return [Workspace.from_row(r) for r in rows]

    def count_owner(
        self,
        owner_id: str,
        statuses: Iterable[str] | None = None,
        *,
        exclude: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM workspaces WHERE owner_id = ?"
        params: list[Any] = [owner_id]
        if statuses is None:
            sql += " AND status != 'deleted'"
        else:
            sts = list(statuses)
            sql += f" AND status IN ({','.join('?' for _ in sts)})"
            params += sts
        if exclude:
            sql += " AND id != ?"
            params.append(exclude)
        if conn is not None:
            return int(conn.execute(sql, params).fetchone()[0])
        with self.db.connect() as c:
            return int(c.execute(sql, params).fetchone()[0])

    def events(self, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT id, at, event, from_status, to_status, reason, actor_user_id FROM workspace_events "
                "WHERE workspace_id = ? ORDER BY at DESC, id DESC LIMIT ?",
                (workspace_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- instances -----------------------------------------------------------------------
    def current_instance(self, workspace_id: str) -> Instance | None:
        with self.db.connect() as c:
            row = c.execute(
                "SELECT * FROM runtime_instances WHERE workspace_id = ? AND removed_at IS NULL "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        return self._inst(row)

    def live_instances(self, workspace_id: str) -> list[Instance]:
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT * FROM runtime_instances WHERE workspace_id = ? AND removed_at IS NULL",
                (workspace_id,),
            ).fetchall()
        return [i for i in map(self._inst, rows) if i]

    def registered_resources(self) -> dict[tuple[str, str], str]:
        """(runtime, resource id) -> workspace id for every instance not yet removed."""
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT runtime, resource_id, workspace_id FROM runtime_instances WHERE removed_at IS NULL"
            ).fetchall()
        return {(r["runtime"], r["resource_id"]): r["workspace_id"] for r in rows}

    @staticmethod
    def _inst(row: sqlite3.Row | None) -> Instance | None:
        if row is None:
            return None
        return Instance(
            row["id"],
            row["workspace_id"],
            row["runtime"],
            row["resource_id"],
            row["image"],
            row["created_at"],
        )

    def register_instance(
        self, workspace_id: str, runtime: str, resource_id: str, image: str, *, require_status: Iterable[str]
    ) -> bool:
        """Record a freshly created runtime resource, but only if the workspace is still in an
        expected status (it may have been deleted while the job ran). False => caller must remove it."""
        allowed = list(require_status)
        with self.db.transaction() as c:
            row = c.execute("SELECT status FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            if row is None or row["status"] not in allowed:
                return False
            c.execute(
                "INSERT INTO runtime_instances (id, workspace_id, runtime, resource_id, image, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(), workspace_id, runtime, resource_id, image, to_iso(utcnow())),
            )
        return True

    def mark_instance_removed(self, instance_id: str) -> None:
        with self.db.transaction() as c:
            c.execute(
                "UPDATE runtime_instances SET removed_at = ? WHERE id = ?", (to_iso(utcnow()), instance_id)
            )

    # -- state changes -------------------------------------------------------------------
    def add_event(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        event: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        reason: str | None = None,
        actor: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO workspace_events (id, workspace_id, at, event, from_status, to_status, "
            "reason, actor_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                workspace_id,
                to_iso(utcnow()),
                event,
                from_status,
                to_status,
                (reason or None),
                actor,
            ),
        )

    def transition(
        self,
        workspace_id: str,
        to: str,
        *,
        from_: Iterable[str],
        reason: str | None = None,
        event: str | None = None,
        actor: str | None = None,
        sets: dict[str, Any] | None = None,
        extra_check: Any = None,
    ) -> str | None:
        """Atomically move to ``to`` if the current status is one of ``from_`` (and legal).

        Returns the previous status when applied, ``None`` when the workspace was not in an
        eligible status (someone else got there first). ``extra_check(conn, row)`` may veto by
        returning False or raise to abort (used for quota checks under the write lock).
        """
        requested = list(from_)
        sources = [s for s in requested if lifecycle.can_transition(s, to)]
        if not sources:
            raise lifecycle.InvalidTransition(",".join(requested), to)
        extras = dict(sets or {})
        if not set(extras) <= _SETTABLE:
            raise ValueError("unsupported column in transition")
        now = to_iso(utcnow())
        with self.db.transaction() as c:
            row = c.execute("SELECT status FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            if row is None or row["status"] not in sources:
                return None
            if extra_check is not None and extra_check(c, row) is False:
                return None
            previous = row["status"]
            assignments = ["status = ?", "status_reason = ?", "updated_at = ?"]
            values: list[Any] = [to, reason, now]
            for col, val in extras.items():
                assignments.append(f"{col} = ?")
                values.append(val)
            c.execute(f"UPDATE workspaces SET {', '.join(assignments)} WHERE id = ?", [*values, workspace_id])  # noqa: S608
            self.add_event(
                c,
                workspace_id,
                event or f"status.{to}",
                from_status=previous,
                to_status=to,
                reason=reason,
                actor=actor,
            )
        return previous

    def insert(
        self,
        values: dict[str, Any],
        volume: dict[str, Any],
        *,
        max_workspaces: int,
        max_running: int,
        actor: str | None,
    ) -> Workspace:
        """Insert a workspace + its volume row atomically, enforcing both quotas.

        The counts and the insert share one BEGIN IMMEDIATE transaction, so two concurrent creates
        cannot both slip under a limit. Raises QuotaExceeded; a name clash raises
        sqlite3.IntegrityError (the caller maps it).
        """
        now = to_iso(utcnow())
        owner = values["owner_id"]
        with self.db.transaction() as c:
            if self.count_owner(owner, conn=c) >= max_workspaces:
                raise QuotaExceeded("workspaces", max_workspaces)
            if self.count_owner(owner, lifecycle.ACTIVE, conn=c) >= max_running:
                raise QuotaExceeded("running", max_running)
            cols = {**values, "created_at": now, "updated_at": now}
            cols["labels"] = json.dumps(cols.get("labels", {}), separators=(",", ":"))
            cols["extra_caps"] = json.dumps(list(cols.get("extra_caps", ())), separators=(",", ":"))
            c.execute(
                f"INSERT INTO workspaces ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",  # noqa: S608
                list(cols.values()),
            )
            vol = {**volume, "workspace_id": values["id"], "created_at": now, "updated_at": now}
            c.execute(
                f"INSERT INTO storage_volumes ({', '.join(vol)}) VALUES ({', '.join('?' for _ in vol)})",  # noqa: S608
                list(vol.values()),
            )
            self.add_event(c, values["id"], "created", to_status=values["status"], actor=actor)
            created = self.get(values["id"], c)
            assert created is not None
            return created

    def update_fields(self, workspace_id: str, **fields: Any) -> None:
        allowed = {
            "name",
            "labels",
            "idle_timeout_minutes",
            "last_activity_at",
            "last_health_at",
            "last_health_ok",
        }
        if not set(fields) <= allowed:
            raise ValueError("unsupported column")
        if "labels" in fields:
            fields["labels"] = json.dumps(fields["labels"], separators=(",", ":"))
        assignments = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        with self.db.transaction() as c:
            c.execute(
                f"UPDATE workspaces SET {assignments} WHERE id = ?",  # noqa: S608
                [*fields.values(), to_iso(utcnow()), workspace_id],
            )

    def set_volume_state(self, workspace_id: str, state: str) -> None:
        now = to_iso(utcnow())
        with self.db.transaction() as c:
            c.execute(
                "UPDATE storage_volumes SET state = ?, updated_at = ?, "
                "purged_at = CASE WHEN ? = 'purged' THEN ? ELSE purged_at END WHERE workspace_id = ?",
                (state, now, state, now, workspace_id),
            )
