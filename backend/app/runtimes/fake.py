"""Deterministic in-memory runtime for tests. Never selectable in production."""

from __future__ import annotations

import itertools
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.runtimes.base import RuntimeAdapter, RuntimeFailure, RuntimeSpec, RuntimeState
from app.runtimes.pty_shell import ShellSession


class FakeShell(ShellSession):
    """Prompt, then echoes input back; ``exit`` ends it. Records resizes."""

    def __init__(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self._out: list[bytes] = [b"$ "]
        self._exit: int | None = None
        self._lock = threading.Lock()
        self.written: list[bytes] = []

    def read(self, timeout: float) -> bytes | None:
        import time

        end = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._out:
                    return self._out.pop(0)
                if self.closed or self._exit is not None:
                    return b""
            if time.monotonic() >= end:
                return None
            time.sleep(0.005)

    def write(self, data: bytes) -> None:
        with self._lock:
            self.written.append(data)
            if data.strip() == b"exit":
                self._exit = 0
            else:
                self._out.append(data)

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)
        self.resizes.append((cols, rows))

    def close(self) -> None:
        with self._lock:
            self.closed = True

    @property
    def exit_code(self) -> int | None:
        return self._exit if self._exit is not None else (0 if self.closed else None)


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
    supports_tor: bool = True
    streams: bool = True
    resources: dict[str, _Res] = field(default_factory=dict)
    gateways: dict[str, dict[str, str]] = field(default_factory=dict)  # helper containers by id
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
        if spec.egress == "tor":
            self._enter("gateway", spec.workspace_id)
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
        self.gateways.pop(resource_id, None)

    def inspect(self, resource_id: str) -> RuntimeState:
        self._enter("inspect", resource_id)
        res = self.resources.get(resource_id)
        if res is None:
            return RuntimeState("missing")
        return RuntimeState(res.state, res.healthy, res.exit_code)  # type: ignore[arg-type]

    shells: list[FakeShell] = field(default_factory=list)

    def open_shell(self, resource_id: str, cols: int, rows: int) -> ShellSession:
        self._enter("open_shell", resource_id)
        res = self._get(resource_id)
        if res.state != "running":
            raise RuntimeFailure("workspace is not running", operation="shell")
        shell = FakeShell(cols, rows)
        self.shells.append(shell)
        return shell

    images: set[str] | None = None  # None: every image counts as present

    def has_image(self, image: str) -> bool | None:
        return True if self.images is None else image in self.images

    def stream_bridge_argv(self, resource_id: str, port: int) -> tuple[list[str], dict[str, str]]:
        self._enter("stream_bridge", resource_id)
        res = self._get(resource_id)
        if res.state != "running":
            raise RuntimeFailure("workspace is not running", operation="stream")
        # A real subprocess that echoes every byte back, like a trivial VNC peer.
        code = (
            "import sys\nd=sys.stdin.buffer\no=sys.stdout.buffer\n"
            "while True:\n c=d.read1(65536)\n if not c:break\n o.write(c);o.flush()"
        )
        return ([sys.executable, "-c", code], {})

    def asset_argv(self, resource_id: str, path: str) -> tuple[list[str], dict[str, str]]:
        self._enter("asset", resource_id)
        res = self._get(resource_id)
        if res.state != "running":
            raise RuntimeFailure("workspace is not running", operation="stream")
        if path in getattr(self, "missing_assets", ()):
            return ([sys.executable, "-c", "import sys;sys.exit(1)"], {})  # like `cat` on a missing file
        if path.endswith("/big.js"):  # larger than any pipe chunk; ends with a marker to detect truncation
            code = "import sys;sys.stdout.buffer.write(b'x' * 300000 + b'//END')"
            return ([sys.executable, "-c", code], {})
        code = "import sys;sys.stdout.buffer.write(b'// fake asset ' + sys.argv[1].encode())"
        return ([sys.executable, "-c", code, path], {})

    def list_managed(self) -> dict[str, dict[str, str]]:
        self._enter("list_managed", "-")
        return {rid: dict(r.spec.labels) for rid, r in self.resources.items()}

    tor_answer: dict[str, object] = field(default_factory=lambda: {"is_tor": True, "exit_ip": "203.0.113.7"})

    def tor_check(self, resource_id: str) -> dict[str, object]:
        self._enter("tor_check", resource_id)
        return dict(self.tor_answer)

    def list_gateways(self) -> dict[str, dict[str, str]]:
        return {rid: dict(labels) for rid, labels in self.gateways.items()}

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
