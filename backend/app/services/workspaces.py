"""Workspace lifecycle service: validation, authorization, state changes, background jobs.

Every state change is an atomic compare-and-set in the store. User-facing operations only flip
the status and queue a job; jobs re-check the status before acting, so a delete, a crash or a
second request can never be overtaken by stale work. Failure reasons shown to users come from
``RuntimeFailure.safe_message`` (no host paths or secrets); anything unexpected becomes a generic
message and the details go to the server log only.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.auth.policy import Perm, can_access_workspace, has_permission
from app.core import errors
from app.core.config import Settings
from app.core.ids import from_iso, new_id, to_iso, utcnow
from app.core.platform import Capabilities
from app.db import Database
from app.runtimes.base import RuntimeAdapter, RuntimeFailure, RuntimeSpec, RuntimeSpecError
from app.services import audit, lifecycle, resources
from app.services import policy as policy_mod
from app.services.audit import RequestContext
from app.services.jobs import JobRunner
from app.services.templates import LAB_RULES_VERSION, Availability, Template, TemplateRegistry, evaluate
from app.services.workspace_store import QuotaExceeded, Workspace, WorkspaceStore
from app.storage.manager import StorageError, StorageManager

if TYPE_CHECKING:
    from app.auth.service import User

log = logging.getLogger("orbitnest.workspaces")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
LABEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,30}\Z")
LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._ -]{0,63}\Z")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
MAX_LABELS = 10
MAX_IDLE_MINUTES = 60 * 24 * 14
PROVISION_MIN_ABANDON_MINUTES = 20  # an image pull may legitimately take a while
LABEL_WORKSPACE = "orbitnest.workspace"
GENERIC_FAILURE = "An unexpected error occurred; see the server log."

AdapterSource = dict[str, RuntimeAdapter] | Callable[[], dict[str, RuntimeAdapter]]


def _invalid(field_name: str, reason: str) -> errors.AppError:
    return errors.AppError(
        errors.VALIDATION_ERROR,
        "Request body is invalid.",
        422,
        [{"field": field_name, "reason": reason, "type": "value_error"}],
    )


def _clean_reason(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()[:200] or None


@dataclass
class MaintenanceReport:
    marked_idle: list[str] = field(default_factory=list)
    stopped_idle: list[str] = field(default_factory=list)
    reconciled: list[dict[str, str]] = field(default_factory=list)
    orphans: list[dict[str, str]] = field(default_factory=list)
    orphans_removed: list[dict[str, str]] = field(default_factory=list)
    orphan_errors: list[dict[str, str]] = field(default_factory=list)
    leftover_data: list[dict[str, str]] = field(default_factory=list)
    leftover_removed: list[str] = field(default_factory=list)
    retained_data: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class WorkspaceService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        store: WorkspaceStore,
        registry: TemplateRegistry,
        storage: StorageManager,
        runtimes: AdapterSource,
        runner: JobRunner,
        capabilities: Callable[[], Capabilities],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], Any] = utcnow,
    ) -> None:
        self.db = db
        self.settings = settings
        self.store = store
        self.registry = registry
        self.storage = storage
        self.runner = runner
        self.capabilities = capabilities
        self._runtimes_source = runtimes
        self._runtimes: dict[str, RuntimeAdapter] | None = None
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._inflight: set[str] = set()
        self._image_cache: dict[str, tuple[float, bool | None]] = {}
        # Called with a workspace id when its runtime is about to stop, reset or go away.
        self.on_inactive: list[Callable[[str], None]] = []
        self._lock = threading.Lock()

    # -- plumbing ------------------------------------------------------------------------
    def adapters(self) -> dict[str, RuntimeAdapter]:
        if self._runtimes is None:
            src = self._runtimes_source
            self._runtimes = dict(src() if callable(src) else src)
        return self._runtimes

    def _image_present(self, template: Template) -> bool | None:
        """Cached (10 s) check that a locally built template image exists."""
        now = time.monotonic()
        cached = self._image_cache.get(template.image)
        if cached and now - cached[0] < 10:
            return cached[1]
        adapter = self.adapters().get("docker")
        found = adapter.has_image(template.image) if adapter else None
        self._image_cache[template.image] = (now, found)
        return found

    def availability(self, template: Template) -> Availability:
        return evaluate(template, self.capabilities(), image_present=self._image_present)

    def _notify_inactive(self, workspace_id: str) -> None:
        for hook in self.on_inactive:
            try:
                hook(workspace_id)
            except Exception:
                log.exception("inactive hook failed", extra={"workspace": workspace_id})

    def _submit(self, fn: Callable[..., None], workspace_id: str, *args: object) -> None:
        with self._lock:
            self._inflight.add(workspace_id)

        def run() -> None:
            try:
                fn(workspace_id, *args)
            finally:
                with self._lock:
                    self._inflight.discard(workspace_id)

        run.__name__ = getattr(fn, "__name__", "job")
        self.runner.submit(run)

    def _audit(
        self,
        actor: User | None,
        action: str,
        outcome: str,
        ws_id: str | None,
        ctx: RequestContext | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        audit.record(
            self.db,
            action=f"workspace.{action}",
            outcome=outcome,
            actor_user_id=actor.id if actor else None,
            target_type="workspace",
            target_id=ws_id,
            ctx=ctx,
            details=details,
        )

    def _load(self, actor: User, workspace_id: str, *, write: bool, ctx: RequestContext | None) -> Workspace:
        """Fetch with ownership enforced. Foreign and missing workspaces are indistinguishable (404)."""
        ws = self.store.get(workspace_id) if storage_id_ok(workspace_id) else None
        if ws is None:
            raise errors.AppError(errors.NOT_FOUND, "Workspace not found.", 404)
        if not can_access_workspace(user_id=actor.id, role=actor.role, owner_id=ws.owner_id, write=write):
            self._audit(actor, "access_denied", "denied", ws.id, ctx, {"write": write})
            raise errors.AppError(errors.NOT_FOUND, "Workspace not found.", 404)
        return ws

    def _busy(self, workspace_id: str, wanted: str) -> errors.AppError:
        ws = self.store.get(workspace_id)
        if ws is not None and ws.status == "deleted":
            return errors.AppError(errors.WORKSPACE_DELETED, "This workspace has been deleted.", 409)
        status = ws.status if ws else "unknown"
        return errors.AppError(
            errors.WORKSPACE_BUSY,
            f"Cannot {wanted} a workspace that is {status}.",
            409,
            [{"status": status}],
        )

    def _fail(self, workspace_id: str, from_: list[str], reason: str | None, event: str = "failed") -> None:
        self.store.transition(workspace_id, "failed", from_=from_, reason=_clean_reason(reason), event=event)

    def _running_quota(self, owner_id: str, exclude: str | None) -> Callable[[sqlite3.Connection, Any], bool]:
        limit = policy_mod.load(self.db).max_running_per_user

        def check(conn: sqlite3.Connection, _row: Any) -> bool:
            if self.store.count_owner(owner_id, lifecycle.ACTIVE, exclude=exclude, conn=conn) >= limit:
                raise errors.AppError(
                    errors.SESSION_LIMIT,
                    f"You can run at most {limit} workspaces at once. Stop one first.",
                    409,
                )
            return True

        return check

    # -- validation ----------------------------------------------------------------------
    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or name != name.strip() or not NAME_RE.match(name):
            raise _invalid(
                "name",
                "1-64 characters: letters, digits, spaces, '.', '_', '-'; start with a letter or digit",
            )
        return name

    @staticmethod
    def _validate_labels(labels: dict[str, str]) -> dict[str, str]:
        if len(labels) > MAX_LABELS:
            raise _invalid("labels", f"at most {MAX_LABELS} labels")
        for k, v in labels.items():
            if not LABEL_KEY_RE.match(k) or not isinstance(v, str) or not LABEL_VALUE_RE.match(v):
                raise _invalid(
                    "labels", "label keys are lowercase letters/digits/._-, values are short plain text"
                )
        return dict(labels)

    # -- reads ---------------------------------------------------------------------------
    def access(
        self, actor: User, workspace_id: str, *, write: bool, ctx: RequestContext | None = None
    ) -> Workspace:
        """Ownership-checked fetch for other services (files, terminal). Foreign ids are 404."""
        return self._load(actor, workspace_id, write=write, ctx=ctx)

    def get(self, actor: User, workspace_id: str, ctx: RequestContext | None = None) -> Workspace:
        return self._load(actor, workspace_id, write=False, ctx=ctx)

    def list(
        self,
        actor: User,
        *,
        all_users: bool = False,
        status: str | None = None,
        template_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Workspace], int]:
        if all_users and actor.role != "admin":
            raise errors.AppError(errors.FORBIDDEN, "You do not have permission to do that.", 403)
        if status is not None and status not in lifecycle.STATES:
            raise _invalid("status", "unknown status")
        return self.store.list(
            owner_id=None if all_users else actor.id,
            status=status,
            template_id=template_id,
            query=query,
            limit=limit,
            offset=offset,
        )

    def events(
        self, actor: User, workspace_id: str, ctx: RequestContext | None = None
    ) -> list[dict[str, Any]]:
        ws = self._load(actor, workspace_id, write=False, ctx=ctx)
        return self.store.events(ws.id)

    # -- create --------------------------------------------------------------------------
    def create(
        self,
        actor: User,
        *,
        name: str,
        template_id: str,
        profile: str | None = None,
        persistence: str | None = None,
        network: str | None = None,
        idle_timeout_minutes: int | None = None,
        labels: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        acknowledge_authorized_use: bool = False,
        capabilities: list[str] | None = None,
        ctx: RequestContext | None = None,
    ) -> tuple[Workspace, bool]:
        """Returns (workspace, created). ``created`` is False when an idempotency key replayed."""
        if not has_permission(actor.role, Perm.WORKSPACE_WRITE):
            raise errors.AppError(errors.FORBIDDEN, "You do not have permission to do that.", 403)
        name = self._validate_name(name)
        label_map = self._validate_labels(labels or {})
        if idempotency_key is not None:
            if not IDEMPOTENCY_RE.match(idempotency_key):
                raise _invalid("idempotency_key", "8-64 characters: letters, digits, '_' or '-'")
            existing = self.store.by_idempotency_key(actor.id, idempotency_key)
            if existing is not None:
                return existing, False

        template = self.registry.get(template_id)
        if template is None:
            raise errors.AppError(errors.NOT_FOUND, "Template not found.", 404)
        avail = self.availability(template)
        adapter = self.adapters().get(avail.runtime or "")
        if not avail.creatable or adapter is None:
            reason = avail.reason or "No runtime is available for this template on this host."
            self._audit(
                actor, "create", "failure", None, ctx, {"template": template.id, "why": "unavailable"}
            )
            raise errors.AppError(
                errors.TEMPLATE_UNAVAILABLE,
                reason,
                409,
                [{"remediation": avail.remediation}] if avail.remediation else None,
            )
        if template.authorized_use_only and not acknowledge_authorized_use:
            raise errors.AppError(
                errors.CONFIRMATION_REQUIRED,
                "This profile is for authorized security testing only. Confirm that you have permission.",
                422,
            )

        extra_caps = self._validate_extra_caps(template, capabilities)
        profile_name = profile or template.default_profile
        try:
            res = resources.resolve_profile(profile_name, max_profile=template.max_profile)
        except resources.ProfileError as exc:
            raise _invalid("profile", str(exc)) from None
        if template.min_profile and resources.profile_rank(profile_name) < resources.profile_rank(
            template.min_profile
        ):
            raise _invalid(
                "profile", f"{template.name} needs at least the {template.min_profile!r} size to run properly"
            )
        persistence = persistence or template.default_persistence
        if persistence not in ("persistent", "disposable"):
            raise _invalid("persistence", "must be persistent or disposable")
        network = network or template.network_policy
        if network not in template.allowed_networks:
            raise _invalid("network", f"this template allows: {', '.join(template.allowed_networks)}")
        if idle_timeout_minutes is None:
            idle: int | None = template.default_idle_minutes
        elif idle_timeout_minutes == 0:
            idle = None
        elif 1 <= idle_timeout_minutes <= MAX_IDLE_MINUTES:
            idle = idle_timeout_minutes
        else:
            raise _invalid("idle_timeout_minutes", f"0 (never) or 1-{MAX_IDLE_MINUTES}")

        wid = new_id()
        policy = policy_mod.load(self.db)
        values = {
            "id": wid,
            "owner_id": actor.id,
            "name": name,
            "template_id": template.id,
            "runtime": avail.runtime,
            "status": "provisioning",
            "persistence": persistence,
            "destroy_on_stop": int(persistence == "disposable"),
            "resource_profile": profile_name,
            "cpu_millis": res.cpu_millis,
            "memory_mb": res.memory_mb,
            "pids_limit": res.pids,
            "storage_quota_mb": res.storage_mb,
            "idle_timeout_minutes": idle,
            "network_policy": network,
            "labels": label_map,
            "extra_caps": extra_caps,
            "idempotency_key": idempotency_key,
        }
        volume = {
            "id": new_id(),
            "kind": "bind",
            "host_path": StorageManager.relative_path(wid),
            "mount_path": template.data_mount,
            "persistence": persistence,
            "quota_mb": res.storage_mb,
            "state": "active",
        }
        try:
            ws = self.store.insert(
                values,
                volume,
                max_workspaces=policy.max_workspaces_per_user,
                max_running=policy.max_running_per_user,
                actor=actor.id,
            )
        except QuotaExceeded as exc:
            self._audit(actor, "create", "denied", None, ctx, {"quota": exc.kind})
            if exc.kind == "workspaces":
                raise errors.AppError(
                    errors.QUOTA_EXCEEDED,
                    f"You have reached the limit of {exc.limit} workspaces. Delete one first.",
                    409,
                ) from None
            raise errors.AppError(
                errors.SESSION_LIMIT,
                f"You can run at most {exc.limit} workspaces at once. Stop one first.",
                409,
            ) from None
        except sqlite3.IntegrityError:
            if idempotency_key is not None:
                replay = self.store.by_idempotency_key(actor.id, idempotency_key)
                if replay is not None:
                    return replay, False
            raise errors.AppError(
                errors.CONFLICT, "You already have a workspace with that name.", 409
            ) from None
        self._audit(
            actor,
            "create",
            "success",
            ws.id,
            ctx,
            {
                "template": template.id,
                "profile": profile_name,
                "runtime": ws.runtime,
                **(
                    {"lab_rules_accepted": LAB_RULES_VERSION, "capabilities": list(extra_caps)}
                    if template.authorized_use_only
                    else {}
                ),
            },
        )
        self._submit(self._provision, ws.id)
        return ws, True

    # -- user operations -----------------------------------------------------------------
    def start(self, actor: User, workspace_id: str, ctx: RequestContext | None = None) -> Workspace:
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        self._ensure_runnable(ws)
        adapter = self.adapters().get(ws.runtime)
        inst = self.store.current_instance(ws.id)
        if inst is not None and adapter is not None:
            try:
                if adapter.inspect(inst.resource_id).state == "missing":
                    self.store.mark_instance_removed(inst.id)
                    inst = None
            except RuntimeFailure:
                pass  # cannot tell; the start job reports a precise failure if it is really gone
        target = "starting" if inst is not None else "provisioning"
        prev = self.store.transition(
            ws.id,
            target,
            from_=["stopped", "failed"],
            event="start",
            actor=actor.id,
            extra_check=self._running_quota(ws.owner_id, ws.id),
        )
        if prev is None:
            raise self._busy(ws.id, "start")
        self._audit(actor, "start", "success", ws.id, ctx)
        self._submit(self._start_job if target == "starting" else self._provision, ws.id)
        return self.store.get(ws.id) or ws

    def stop(self, actor: User, workspace_id: str, ctx: RequestContext | None = None) -> Workspace:
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        prev = self.store.transition(
            ws.id, "stopping", from_=["starting", "running", "idle", "failed"], event="stop", actor=actor.id
        )
        if prev is None:
            raise self._busy(ws.id, "stop")
        self._audit(actor, "stop", "success", ws.id, ctx)
        self._submit(self._stop_job, ws.id, False)
        return self.store.get(ws.id) or ws

    def restart(self, actor: User, workspace_id: str, ctx: RequestContext | None = None) -> Workspace:
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        self._ensure_runnable(ws)
        prev = self.store.transition(
            ws.id, "stopping", from_=["running", "idle"], event="restart", actor=actor.id
        )
        if prev is None:
            raise self._busy(ws.id, "restart")
        self._audit(actor, "restart", "success", ws.id, ctx)
        self._submit(self._stop_job, ws.id, True)
        return self.store.get(ws.id) or ws

    def reset(
        self,
        actor: User,
        workspace_id: str,
        *,
        wipe_data: bool = False,
        confirm_name: str | None = None,
        ctx: RequestContext | None = None,
    ) -> Workspace:
        """Re-create the runtime instance from its template. Data survives unless wiped explicitly."""
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        if wipe_data and confirm_name != ws.name:
            raise errors.AppError(
                errors.CONFIRMATION_REQUIRED, "Type the workspace name to confirm wiping its data.", 422
            )
        self._ensure_runnable(ws)
        prev = self.store.transition(
            ws.id,
            "provisioning",
            from_=["stopped", "failed"],
            event="reset",
            actor=actor.id,
            extra_check=self._running_quota(ws.owner_id, ws.id),
        )
        if prev is None:
            raise self._busy(ws.id, "reset")
        self._audit(actor, "reset", "success", ws.id, ctx, {"wipe_data": wipe_data})
        self._submit(self._reset_job, ws.id, wipe_data)
        return self.store.get(ws.id) or ws

    def delete(
        self,
        actor: User,
        workspace_id: str,
        *,
        delete_data: bool = False,
        confirm_name: str | None = None,
        ctx: RequestContext | None = None,
    ) -> Workspace:
        """Delete the workspace. Persistent data is retained unless ``delete_data`` and the exact
        name are given; disposable workspaces always lose their data."""
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        purge = ws.persistence == "disposable" or delete_data
        if ws.persistence == "persistent" and delete_data and confirm_name != ws.name:
            raise errors.AppError(
                errors.CONFIRMATION_REQUIRED, "Type the workspace name to confirm deleting its data.", 422
            )
        sources = [s for s in lifecycle.STATES if s not in ("deleting", "deleted")]
        prev = self.store.transition(
            ws.id, "deleting", from_=sources, event="delete", actor=actor.id, reason=None
        )
        if prev is None:
            raise self._busy(ws.id, "delete")
        self._audit(actor, "delete", "success", ws.id, ctx, {"purge_data": purge, "from": prev})
        self._submit(self._delete_job, ws.id, purge)
        return self.store.get(ws.id) or ws

    def update(
        self,
        actor: User,
        workspace_id: str,
        *,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        idle_timeout_minutes: int | None = None,
        ctx: RequestContext | None = None,
    ) -> Workspace:
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        if ws.status in ("deleting", "deleted"):
            raise self._busy(ws.id, "change")
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = self._validate_name(name)
        if labels is not None:
            fields["labels"] = self._validate_labels(labels)
        if idle_timeout_minutes is not None:
            if idle_timeout_minutes == 0:
                fields["idle_timeout_minutes"] = None
            elif 1 <= idle_timeout_minutes <= MAX_IDLE_MINUTES:
                fields["idle_timeout_minutes"] = idle_timeout_minutes
            else:
                raise _invalid("idle_timeout_minutes", f"0 (never) or 1-{MAX_IDLE_MINUTES}")
        if not fields:
            raise _invalid("body", "nothing to change")
        try:
            self.store.update_fields(ws.id, **fields)
        except sqlite3.IntegrityError:
            raise errors.AppError(
                errors.CONFLICT, "You already have a workspace with that name.", 409
            ) from None
        self._audit(actor, "update", "success", ws.id, ctx, {"fields": sorted(fields)})
        return self.store.get(ws.id) or ws

    def touch_activity(self, actor: User, workspace_id: str, ctx: RequestContext | None = None) -> Workspace:
        """Record user activity (resets the idle clock; wakes an idle workspace's status)."""
        ws = self._load(actor, workspace_id, write=True, ctx=ctx)
        now = to_iso(self._now())
        if ws.status == "idle":
            self.store.transition(
                ws.id, "running", from_=["idle"], event="active", sets={"last_activity_at": now}
            )
        elif ws.status == "running":
            self.store.update_fields(ws.id, last_activity_at=now)
        return self.store.get(ws.id) or ws

    def _ensure_runnable(self, ws: Workspace) -> None:
        template = self.registry.get(ws.template_id)
        if template is None:
            raise errors.AppError(
                errors.TEMPLATE_UNAVAILABLE, "This workspace's template no longer exists.", 409
            )
        avail = self.availability(template)
        if not avail.creatable or ws.runtime not in self.adapters():
            raise errors.AppError(
                errors.TEMPLATE_UNAVAILABLE,
                avail.reason or "The runtime for this workspace is not available on this host.",
                409,
            )

    @staticmethod
    def _validate_extra_caps(template: Template, requested: list[str] | None) -> tuple[str, ...]:
        caps = tuple(dict.fromkeys(requested or ()))
        if not caps:
            return ()
        offered = set(template.optional_capabilities)
        bad = [c for c in caps if c not in offered]
        if bad:
            raise _invalid("capabilities", f"not available for this template: {bad[0]}")
        return caps

    # -- jobs ----------------------------------------------------------------------------
    @staticmethod
    def _spec(ws: Workspace, template: Template, data_dir: str) -> RuntimeSpec:
        image = f"{template.image}@{template.image_digest}" if template.image_digest else template.image
        return RuntimeSpec(
            workspace_id=ws.id,
            image=image,
            command=template.command,
            cpu_millis=ws.cpu_millis,
            memory_mb=ws.memory_mb,
            pids=ws.pids_limit,
            network=ws.network_policy,
            mount_path=template.data_mount,
            data_dir=data_dir,
            capabilities=tuple(dict.fromkeys((*template.capabilities, *ws.extra_caps))),
            healthcheck=template.healthcheck,
            local_command=template.local_command,
            shm_mb=template.shm_mb,
            stream_port=template.stream_port,
            run_as_root=template.run_as_root,
            labels={LABEL_WORKSPACE: ws.id},
        )

    def _provision(self, workspace_id: str) -> None:
        ws = self.store.get(workspace_id)
        if ws is None or ws.status != "provisioning":
            return
        adapter = self.adapters().get(ws.runtime)
        template = self.registry.get(ws.template_id)
        if adapter is None or template is None:
            self._fail(workspace_id, ["provisioning"], "The runtime or template is no longer available.")
            return
        try:
            data_dir = self.storage.create(workspace_id)
            spec = self._spec(ws, template, str(data_dir))
            resource_id = adapter.create(spec)
        except RuntimeFailure as exc:
            self._fail(workspace_id, ["provisioning"], exc.safe_message)
            return
        except (RuntimeSpecError, StorageError) as exc:
            self._fail(workspace_id, ["provisioning"], str(exc))
            return
        except Exception:
            log.exception("provisioning crashed", extra={"workspace": workspace_id})
            self._fail(workspace_id, ["provisioning"], GENERIC_FAILURE)
            return
        if not self.store.register_instance(
            workspace_id, ws.runtime, resource_id, spec.image, require_status=["provisioning"]
        ):
            self._best_effort_remove(adapter, resource_id)  # deleted while we were creating
            return
        if (
            self.store.transition(workspace_id, "starting", from_=["provisioning"], event="provisioned")
            is None
        ):
            return
        self._start_job(workspace_id)

    def _start_job(self, workspace_id: str) -> None:
        ws = self.store.get(workspace_id)
        if ws is None or ws.status != "starting":
            return
        adapter = self.adapters().get(ws.runtime)
        template = self.registry.get(ws.template_id)
        inst = self.store.current_instance(workspace_id)
        if adapter is None or template is None or inst is None:
            self._fail(workspace_id, ["starting"], "No runtime instance is available; use reset.")
            return
        try:
            adapter.start(inst.resource_id)
            problem = self._wait_ready(adapter, inst.resource_id, template)
        except RuntimeFailure as exc:
            problem = exc.safe_message
        except Exception:
            log.exception("start crashed", extra={"workspace": workspace_id})
            problem = GENERIC_FAILURE
        if problem is not None:
            self._best_effort_stop(adapter, inst.resource_id)
            self._fail(workspace_id, ["starting"], problem)
            return
        now = to_iso(self._now())
        self.store.transition(
            workspace_id,
            "running",
            from_=["starting"],
            event="started",
            sets={"started_at": now, "last_activity_at": now, "last_health_at": now, "last_health_ok": 1},
        )

    def _wait_ready(self, adapter: RuntimeAdapter, resource_id: str, template: Template) -> str | None:
        """None when ready, otherwise a user-safe reason."""
        timeout = self.settings.start_timeout_seconds
        deadline = self._clock() + timeout
        while True:
            state = adapter.inspect(resource_id)
            if state.state == "exited":
                return f"The workspace exited during start (code {state.exit_code})."
            if state.state == "missing":
                return "The runtime resource disappeared during start."
            if state.state == "running":
                if state.healthy is True or (state.healthy is None and not template.healthcheck):
                    return None
                if state.healthy is False:
                    return "The workspace health check failed."
            if self._clock() >= deadline:
                return f"The workspace did not become ready within {timeout} seconds."
            self._sleep(0.5)

    def _stop_job(self, workspace_id: str, restart: bool = False) -> None:
        ws = self.store.get(workspace_id)
        if ws is None or ws.status != "stopping":
            return
        adapter = self.adapters().get(ws.runtime)
        inst = self.store.current_instance(workspace_id)
        self._notify_inactive(workspace_id)
        try:
            if inst is not None:
                if adapter is None:
                    raise RuntimeFailure("The runtime for this workspace is not available.")
                if adapter.inspect(inst.resource_id).state == "missing":
                    self.store.mark_instance_removed(inst.id)
                else:
                    adapter.stop(inst.resource_id, self.settings.stop_grace_seconds)
                    if ws.destroy_on_stop:
                        adapter.remove(inst.resource_id)
                        self.store.mark_instance_removed(inst.id)
                if ws.destroy_on_stop and ws.persistence == "disposable":
                    self.storage.wipe_contents(workspace_id)
        except RuntimeFailure as exc:
            self._fail(workspace_id, ["stopping"], exc.safe_message)
            return
        except Exception:
            log.exception("stop crashed", extra={"workspace": workspace_id})
            self._fail(workspace_id, ["stopping"], GENERIC_FAILURE)
            return
        if self.store.transition(workspace_id, "stopped", from_=["stopping"], event="stopped") is None:
            return
        if restart:
            self._restart_tail(workspace_id)

    def _restart_tail(self, workspace_id: str) -> None:
        inst = self.store.current_instance(workspace_id)
        target = "starting" if inst is not None else "provisioning"
        ws = self.store.get(workspace_id)
        if ws is None:
            return
        # The user's running slot was held until now; re-check it atomically.
        try:
            prev = self.store.transition(
                workspace_id,
                target,
                from_=["stopped"],
                event="restart",
                extra_check=self._running_quota(ws.owner_id, workspace_id),
            )
        except errors.AppError as exc:
            self._fail(workspace_id, ["stopped"], exc.message)
            return
        if prev is None:
            return
        (self._start_job if target == "starting" else self._provision)(workspace_id)

    def _reset_job(self, workspace_id: str, wipe: bool) -> None:
        ws = self.store.get(workspace_id)
        if ws is None or ws.status != "provisioning":
            return
        adapter = self.adapters().get(ws.runtime)
        self._notify_inactive(workspace_id)
        try:
            for inst in self.store.live_instances(workspace_id):
                if adapter is None:
                    raise RuntimeFailure("The runtime for this workspace is not available.")
                adapter.remove(inst.resource_id)
                self.store.mark_instance_removed(inst.id)
            if wipe:
                self.storage.wipe_contents(workspace_id)
        except RuntimeFailure as exc:
            self._fail(workspace_id, ["provisioning"], exc.safe_message)
            return
        except OSError:
            log.exception("reset could not wipe data", extra={"workspace": workspace_id})
            self._fail(workspace_id, ["provisioning"], "Could not wipe the workspace data.")
            return
        self._provision(workspace_id)

    def _delete_job(self, workspace_id: str, purge: bool) -> None:
        ws = self.store.get(workspace_id)
        if ws is None or ws.status != "deleting":
            return
        adapter = self.adapters().get(ws.runtime)
        self._notify_inactive(workspace_id)
        try:
            for inst in self.store.live_instances(workspace_id):
                if adapter is None:
                    raise RuntimeFailure("The runtime for this workspace is not available; cannot remove it.")
                adapter.remove(inst.resource_id)
                self.store.mark_instance_removed(inst.id)
            if purge:
                self.storage.purge(workspace_id)
                self.store.set_volume_state(workspace_id, "purged")
            else:
                self.store.set_volume_state(workspace_id, "retained")
        except RuntimeFailure as exc:
            self._fail(workspace_id, ["deleting"], exc.safe_message)
            return
        except OSError:
            log.exception("delete could not remove data", extra={"workspace": workspace_id})
            self._fail(workspace_id, ["deleting"], "Could not remove the workspace data.")
            return
        self.store.transition(
            workspace_id,
            "deleted",
            from_=["deleting"],
            event="deleted",
            sets={"deleted_at": to_iso(self._now())},
        )

    @staticmethod
    def _best_effort_remove(adapter: RuntimeAdapter, resource_id: str) -> None:
        try:
            adapter.remove(resource_id)
        except Exception:
            log.warning("best-effort remove failed", exc_info=True)

    @staticmethod
    def _best_effort_stop(adapter: RuntimeAdapter, resource_id: str) -> None:
        try:
            adapter.stop(resource_id, 5)
        except Exception:
            log.warning("best-effort stop failed", exc_info=True)

    # -- maintenance ---------------------------------------------------------------------
    def sweep_idle(self, report: MaintenanceReport | None = None) -> MaintenanceReport:
        report = report or MaintenanceReport()
        pol = policy_mod.load(self.db)
        now = self._now()
        for ws in self.store.with_status(["running", "idle"]):
            last = from_iso(ws.last_activity_at or ws.started_at or ws.updated_at)
            quiet = now - last
            if (
                ws.idle_timeout_minutes is not None
                and pol.idle_action == "stop"
                and quiet >= timedelta(minutes=ws.idle_timeout_minutes)
            ):
                prev = self.store.transition(
                    ws.id,
                    "stopping",
                    from_=["running", "idle"],
                    event="idle_stop",
                    reason="Stopped after being idle.",
                )
                if prev is not None:
                    self._audit(None, "idle_stop", "success", ws.id, None)
                    self._submit(self._stop_job, ws.id, False)
                    report.stopped_idle.append(ws.id)
            elif ws.status == "running" and quiet >= timedelta(minutes=pol.idle_mark_minutes):
                if self.store.transition(ws.id, "idle", from_=["running"], event="idle") is not None:
                    report.marked_idle.append(ws.id)
        return report

    def reconcile(self, *, boot: bool = False, report: MaintenanceReport | None = None) -> MaintenanceReport:
        """Repair state that no longer matches reality. ``boot`` means no job from a previous
        process can still be running, so every transitional workspace is abandoned."""
        report = report or MaintenanceReport()
        now = self._now()
        if boot:
            with self._lock:
                self._inflight.clear()
        for ws in self.store.with_status(sorted(lifecycle.TRANSITIONAL)):
            with self._lock:
                if ws.id in self._inflight:
                    continue
            minutes = self.settings.abandoned_after_minutes
            if ws.status == "provisioning":
                minutes = max(minutes, PROVISION_MIN_ABANDON_MINUTES)
            if not boot and now - from_iso(ws.updated_at) < timedelta(minutes=minutes):
                continue
            reason = {
                "provisioning": "Creation was interrupted. Use Start to retry.",
                "starting": "Start was interrupted. Use Start to retry.",
                "stopping": "Stop was interrupted. Use Stop to retry.",
                "deleting": "Deletion was interrupted. Delete again to finish it.",
            }[ws.status]
            if self.store.transition(ws.id, "failed", from_=[ws.status], reason=reason, event="reconciled"):
                report.reconciled.append({"workspace_id": ws.id, "was": ws.status, "action": "failed"})
        for ws in self.store.with_status(sorted(lifecycle.LIVE)):
            adapter = self.adapters().get(ws.runtime)
            inst = self.store.current_instance(ws.id)
            if adapter is None:
                continue  # runtime unavailable right now (e.g. Docker stopped): do not guess
            if inst is None:
                self.store.transition(
                    ws.id,
                    "failed",
                    from_=[ws.status],
                    reason="The runtime instance record is missing.",
                    event="runtime_lost",
                )
                report.reconciled.append({"workspace_id": ws.id, "was": ws.status, "action": "failed"})
                continue
            try:
                state = adapter.inspect(inst.resource_id)
            except RuntimeFailure:
                continue
            if state.state in ("exited", "missing"):
                why = (
                    "The runtime resource is missing."
                    if state.state == "missing"
                    else f"The workspace stopped unexpectedly (code {state.exit_code})."
                )
                if state.state == "missing":
                    self.store.mark_instance_removed(inst.id)
                if self.store.transition(
                    ws.id, "failed", from_=["running", "idle"], reason=why, event="runtime_lost"
                ):
                    self._notify_inactive(ws.id)
                    report.reconciled.append({"workspace_id": ws.id, "was": ws.status, "action": "failed"})
            elif state.state == "running":
                self.store.update_fields(
                    ws.id, last_health_at=to_iso(now), last_health_ok=int(state.healthy is not False)
                )
        return report

    def find_orphans(self, *, boot: bool = False) -> list[dict[str, str]]:
        registered = self.store.registered_resources()
        found: list[dict[str, str]] = []
        for runtime, adapter in self.adapters().items():
            try:
                managed = adapter.list_managed()
            except RuntimeFailure:
                continue
            for rid, labels in managed.items():
                wid = labels.get(LABEL_WORKSPACE, "")
                owner = registered.get((runtime, rid))
                if owner is not None:
                    row = self.store.get(owner)
                    if row is not None and row.status == "deleted":
                        found.append(_orphan(runtime, rid, owner, "its workspace was deleted"))
                    continue
                ws = self.store.get(wid) if wid else None
                if ws is not None and ws.status == "provisioning" and not boot:
                    continue  # being created right now; not yet registered
                found.append(_orphan(runtime, rid, wid, "not registered to any workspace"))
        return found

    def cleanup(self, *, apply: bool = False, boot: bool = False) -> MaintenanceReport:
        """Report (dry run) or remove orphaned runtime resources and purged data directories.

        Retained persistent data and directories with no database record are only ever
        reported, never deleted: they may be the only copy of someone's files.
        """
        report = MaintenanceReport()
        report.orphans = self.find_orphans(boot=boot)
        with self.db.connect() as c:
            vols = {
                r["workspace_id"]: r["state"]
                for r in c.execute("SELECT workspace_id, state FROM storage_volumes")
            }
        for wid in sorted(self.storage.known_ids()):
            state = vols.get(wid)
            if state is None:
                report.leftover_data.append(
                    {"workspace_id": wid, "reason": "no database record (not removed automatically)"}
                )
            elif state == "purged":
                report.leftover_data.append(
                    {"workspace_id": wid, "reason": "marked purged but still on disk"}
                )
            elif state == "retained":
                report.retained_data.append(wid)
        if apply:
            for item in report.orphans:
                adapter = self.adapters().get(item["runtime"])
                if adapter is None:
                    continue
                try:
                    adapter.remove(item["resource_id"])
                    report.orphans_removed.append(item)
                except RuntimeFailure as exc:
                    report.orphan_errors.append({**item, "error": exc.safe_message})
            for item in report.leftover_data:
                if item["reason"].startswith("marked purged"):
                    try:
                        self.storage.purge(item["workspace_id"])
                        report.leftover_removed.append(item["workspace_id"])
                    except OSError:
                        log.warning("could not purge leftover data", exc_info=True)
            self._audit(
                None,
                "cleanup",
                "success",
                None,
                None,
                {
                    "orphans_removed": len(report.orphans_removed),
                    "data_removed": len(report.leftover_removed),
                },
            )
        return report

    def run_maintenance(self, *, boot: bool = False) -> MaintenanceReport:
        """One periodic pass. Orphans are reported (logged), never auto-removed."""
        report = MaintenanceReport()
        self.reconcile(boot=boot, report=report)
        self.sweep_idle(report)
        report.orphans = self.find_orphans(boot=boot)
        if report.orphans:
            log.warning("orphaned runtime resources found", extra={"count": len(report.orphans)})
        return report


def storage_id_ok(workspace_id: str) -> bool:
    from app.storage.manager import ID_RE

    return bool(ID_RE.match(workspace_id))


def _orphan(runtime: str, resource_id: str, workspace_id: str, reason: str) -> dict[str, str]:
    return {"runtime": runtime, "resource_id": resource_id, "workspace_id": workspace_id, "reason": reason}
