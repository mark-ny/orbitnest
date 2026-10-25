"""Choose runtime adapters from the detected platform capabilities."""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.platform import Capabilities
from app.runtimes.base import RuntimeAdapter
from app.runtimes.docker_runtime import DockerRuntime
from app.runtimes.termux_runtime import LocalProcessRuntime


def build_runtimes(settings: Settings, caps: Capabilities) -> dict[str, RuntimeAdapter]:
    """Adapters keyed by the runtime name that ``templates.evaluate`` returns."""
    adapters: dict[str, RuntimeAdapter] = {}
    if caps.mode in {"linux-docker", "windows-docker-desktop"}:
        adapters["docker"] = DockerRuntime(redact=(str(settings.workspaces_root), str(settings.data_dir)))
    elif caps.mode == "termux-local":
        adapters["local"] = LocalProcessRuntime(Path(settings.data_dir) / "runtime-local")
    return adapters
