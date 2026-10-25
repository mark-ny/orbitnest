"""Deterministic in-memory runtime for tests. Never selectable in production."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.runtimes.base import RuntimeAdapter, RuntimeFailure, RuntimeSpec, RuntimeState


@dataclass
class _Res:
    spec: RuntimeSpec
    state: str = "created"
    healthy: bool | None = None
    exit_code: int | None = None


@dataclass
class FakeRuntime(RuntimeAdapter):
    name: str = "fake"
    isolation: str = "container"
    resources: dict[str, _Res] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    # operation -> number of upcoming calls that should fail
    failures: dict[str, int] = field(default_factory=dict)
    # called as hook(operation, resource_id) before the operation runs (for race tests)
    hook: Callable[[str, str], None] | None = None
    health_after_start: bool | None = None
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _enter(self, op: str, rid: str) -> None:
        with self._lock:
            self.calls.append((op, rid))
            remaining = self.failures.get(op, 0)
            if remaining:
                self.failures[op] = remaining - 1
                raise RuntimeFailure(f"{op} failed (injected)", operation=op)
        if self.hook:
            self.hook(op, rid)

    def fail_next(self, op: str, times: int = 1) -> None:
        self.failures[op] = times

    def ops(self, op: str) -> list[str]:
        return [rid for o, rid in self.calls if o == op]

    # -- adapter API ---------------------------------------------------------------------
    def create(self, spec: RuntimeSpec) -> str:
        spec.validate()
        self._enter("create", "-")
        rid = f"fake-{next(self._ids):04d}"
        self.resources[rid] = _Res(spec)
        return rid

    def _get(self, rid: str) -> _Res:
        res = self.resources.get(rid)
        if res is None:
            raise RuntimeFailure("resource not found")
        return res

    def start(self, resource_id: str) -> None:
        self._enter("start", resource_id)
        res = self._get(resource_id)
        res.state, res.exit_code = "running", None
        res.healthy = self.health_after_start

    def stop(self, resource_id: str, grace_seconds: int) -> None:
        self._enter("stop", resource_id)
        res = self._get(resource_id)
        res.state, res.exit_code = "exited", 0

    def remove(self, resource_id: str) -> None:
        self._enter("remove", resource_id)
        self.resources.pop(resource_id, None)

    def inspect(self, resource_id: str) -> RuntimeState:
        self._enter("inspect", resource_id)
        res = self.resources.get(resource_id)
        if res is None:
            return RuntimeState("missing")
        return RuntimeState(res.state, res.healthy, res.exit_code)  # type: ignore[arg-type]

    def list_managed(self) -> dict[str, dict[str, str]]:
        self._enter("list_managed", "-")
        return {rid: dict(r.spec.labels) for rid, r in self.resources.items()}

    # -- test controls -------------------------------------------------------------------
    def crash(self, rid: str, code: int = 137) -> None:
        self.resources[rid].state, self.resources[rid].exit_code = "exited", code

    def vanish(self, rid: str) -> None:
        self.resources.pop(rid, None)

    def adopt_orphan(self, spec: RuntimeSpec) -> str:
        """A resource the database knows nothing about."""
        rid = f"fake-{next(self._ids):04d}"
        self.resources[rid] = _Res(spec, "running")
        return rid
