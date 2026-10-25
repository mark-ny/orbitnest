"""Periodic maintenance thread: reconcile state, mark/stop idle workspaces, report orphans."""

from __future__ import annotations

import logging
import threading

from app.services.workspaces import WorkspaceService

log = logging.getLogger("orbitnest.maintenance")


class MaintenanceThread:
    def __init__(self, service: WorkspaceService, interval_seconds: int) -> None:
        self.service = service
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Boot reconciliation always runs (jobs of a previous process are gone); the periodic
        loop only when an interval is configured (0 disables it)."""
        try:
            self.service.run_maintenance(boot=True)
        except Exception:
            log.exception("boot reconciliation failed")
        if self.interval <= 0 or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="orbitnest-maintenance", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.service.run_maintenance()
            except Exception:
                log.exception("maintenance pass failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
