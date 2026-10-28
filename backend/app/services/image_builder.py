"""One-click image builds for administrators (the web equivalent of ``python -m app build-images``).

Only built-in template ``build_context`` names are accepted (see ``images.plan``), so a request can
never make this build an arbitrary directory. One build runs at a time; output is kept as a bounded
tail for the UI. Builds are long (tens of minutes to hours on a slow link) and run on a daemon thread.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core import errors
from app.services import images

log = logging.getLogger("orbitnest.images")

MAX_LOG_LINES = 300
MAX_LINE_CHARS = 300
BUILD_TIMEOUT_SECONDS = 6 * 3600
_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_BUILDKIT")

Planner = Callable[[str], list[list[str]]]
Spawner = Callable[[list[str], dict[str, str]], "subprocess.Popen[str]"]


@dataclass
class BuildState:
    context: str
    state: str = "running"  # running | done | failed | cancelled
    step: int = 0
    steps: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    lines: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=MAX_LOG_LINES))

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "state": self.state,
            "step": self.step,
            "steps": self.steps,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "log": list(self.lines),
        }


def _spawn(argv: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - argv list from a fixed plan
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )


class ImageBuilder:
    def __init__(
        self,
        *,
        docker_bin: Callable[[], str] = lambda: "docker",
        planner: Planner | None = None,
        spawn: Spawner = _spawn,
        on_finished: Callable[[], None] = lambda: None,
    ) -> None:
        self._docker_bin = docker_bin
        self._planner = planner
        self._spawn = spawn
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._states: dict[str, BuildState] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._cancel: set[str] = set()

    def _plan(self, context: str) -> list[list[str]]:
        if self._planner is not None:
            return self._planner(context)
        if not images.docker_dir().is_dir():
            raise errors.AppError(
                errors.NOT_SUPPORTED, "The image build files are not available in this installation.", 409
            )
        steps = images.plan([context], docker_bin=self._docker_bin())
        # Plain progress keeps the log readable in the UI.
        return [[s[0], s[1], "--progress=plain", *s[2:]] if s[1:2] == ["build"] else s for s in steps]

    def status(self, context: str) -> dict[str, Any] | None:
        with self._lock:
            st = self._states.get(context)
            return st.to_dict() if st else None

    def running(self) -> str | None:
        with self._lock:
            return next((c for c, s in self._states.items() if s.state == "running"), None)

    def start(self, context: str) -> dict[str, Any]:
        if context not in images.buildable():
            raise errors.AppError(errors.NOT_FOUND, "This template has no buildable image.", 404)
        steps = self._plan(context)
        with self._lock:
            busy = next((c for c, s in self._states.items() if s.state == "running"), None)
            if busy:
                raise errors.AppError(
                    errors.WORKSPACE_BUSY,
                    f"An image build is already running ({busy}). Wait for it to finish.",
                    409,
                )
            state = BuildState(context=context, steps=len(steps))
            self._states[context] = state
        threading.Thread(target=self._run, args=(state, steps), name=f"build-{context}", daemon=True).start()
        return state.to_dict()

    def cancel(self, context: str) -> bool:
        with self._lock:
            proc = self._procs.get(context)
            state = self._states.get(context)
            if state is None or state.state != "running":
                return False
            self._cancel.add(context)
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        return True

    # -- worker ------------------------------------------------------------------------------
    def _run(self, state: BuildState, steps: list[list[str]]) -> None:
        env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
        deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
        try:
            for i, argv in enumerate(steps, start=1):
                state.step = i
                state.lines.append(f"== step {i}/{len(steps)}: {os.path.basename(argv[-1])}")
                try:
                    proc = self._spawn(argv, env)
                except OSError:
                    state.error = "Could not start docker. Is Docker installed and running?"
                    state.state = "failed"
                    return
                with self._lock:
                    self._procs[state.context] = proc
                    cancel_now = state.context in self._cancel  # cancelled before the process existed
                if cancel_now:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
                assert proc.stdout is not None
                for line in proc.stdout:
                    state.lines.append(line.rstrip("\r\n")[:MAX_LINE_CHARS])
                    if time.monotonic() > deadline:
                        proc.kill()
                        state.error = "The build took too long and was stopped."
                        break
                code = proc.wait()
                with self._lock:
                    cancelled = state.context in self._cancel
                if cancelled:
                    state.state, state.error = "cancelled", "Cancelled."
                    return
                if code != 0 or state.error:
                    state.state = "failed"
                    state.error = state.error or f"The build failed (exit code {code}). See the log."
                    return
            state.state = "done"
        except Exception:  # never let the worker die silently
            log.exception("image build crashed", extra={"image": state.context})
            state.state, state.error = "failed", "The build stopped unexpectedly."
        finally:
            with self._lock:
                self._procs.pop(state.context, None)
                self._cancel.discard(state.context)
            state.finished_at = time.time()
            with contextlib.suppress(Exception):
                self._on_finished()
