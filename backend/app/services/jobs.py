"""Background job runners. Lifecycle operations are state transitions executed by a job."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

log = logging.getLogger("orbitnest.jobs")


class JobRunner(Protocol):
    def submit(self, fn: Callable[..., None], *args: object) -> None: ...

    def shutdown(self) -> None: ...


class ThreadRunner:
    """Runs jobs on a small bounded thread pool. Exceptions are logged, never lost silently."""

    def __init__(self, workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="orbitnest-job")

    def submit(self, fn: Callable[..., None], *args: object) -> None:
        def run() -> None:
            try:
                fn(*args)
            except Exception:
                log.exception("job crashed", extra={"job": getattr(fn, "__name__", "job")})

        self._pool.submit(run)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


class InlineRunner:
    """Runs jobs synchronously on submit (deterministic tests, CLI cleanup)."""

    def submit(self, fn: Callable[..., None], *args: object) -> None:
        fn(*args)

    def shutdown(self) -> None:
        pass


class ManualRunner:
    """Queues jobs until ``run_pending`` (tests that must observe intermediate states)."""

    def __init__(self) -> None:
        self.queue: list[tuple[Callable[..., None], tuple[object, ...]]] = []
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., None], *args: object) -> None:
        with self._lock:
            self.queue.append((fn, args))

    def run_pending(self) -> int:
        with self._lock:
            pending, self.queue = self.queue, []
        for fn, args in pending:
            fn(*args)
        return len(pending)

    def drop_pending(self) -> int:
        """Simulate a crash: queued work never runs."""
        with self._lock:
            n = len(self.queue)
            self.queue = []
        return n

    def shutdown(self) -> None:
        pass
