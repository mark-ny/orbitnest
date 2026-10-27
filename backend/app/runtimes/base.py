"""Runtime adapter contract.

Adapters are the only code that touches containers or processes. They receive a validated
:class:`RuntimeSpec` built by the service from a template and a resource profile. There is no
free-form "extra options" channel, so a compromised or buggy caller cannot ask an adapter for a
privileged container, a host mount, a published port or host networking.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Literal

from app.runtimes.pty_shell import ShellSession
from app.services.templates import ALLOWED_CAPABILITIES, IMAGE_RE

WORKSPACE_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")
RuntimeStateName = Literal["created", "running", "exited", "missing", "unknown"]
NETWORKS = frozenset({"none", "isolated-bridge"})


class RuntimeFailure(Exception):
    """A runtime operation failed. ``safe_message`` is shown to users: no host paths or secrets."""

    def __init__(self, safe_message: str, *, operation: str = "") -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.operation = operation


class RuntimeSpecError(ValueError):
    """The spec asked for something no adapter may do."""


@dataclass(frozen=True)
class RuntimeSpec:
    workspace_id: str
    image: str
    command: tuple[str, ...]
    cpu_millis: int
    memory_mb: int
    pids: int
    network: str
    mount_path: str
    data_dir: str | None = None  # absolute host path to bind at mount_path
    capabilities: tuple[str, ...] = ()
    healthcheck: tuple[str, ...] | None = None
    local_command: tuple[str, ...] = ()
    max_file_mb: int = 2048
    shm_mb: int = 64
    stream_port: int | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not WORKSPACE_ID_RE.match(self.workspace_id):
            raise RuntimeSpecError("invalid workspace id")
        if not IMAGE_RE.match(self.image) or ".." in self.image:
            raise RuntimeSpecError("invalid image reference")
        for name, argv in (
            ("command", self.command),
            ("healthcheck", self.healthcheck or ()),
            ("local_command", self.local_command),
        ):
            if len(argv) > 16 or any((not a) or len(a) > 200 or "\x00" in a for a in argv):
                raise RuntimeSpecError(f"invalid {name}")
            if argv and argv[0].startswith("-"):
                raise RuntimeSpecError(f"{name} must start with an executable")
        if self.network not in NETWORKS:
            raise RuntimeSpecError("network policy not allowed")
        if not 100 <= self.cpu_millis <= 4000:
            raise RuntimeSpecError("cpu limit out of range")
        if not 64 <= self.memory_mb <= 8192:
            raise RuntimeSpecError("memory limit out of range")
        if not 16 <= self.pids <= 4096:
            raise RuntimeSpecError("process limit out of range")
        if not set(self.capabilities) <= ALLOWED_CAPABILITIES:
            raise RuntimeSpecError("capability not allowed")
        if not 64 <= self.shm_mb <= 2048:
            raise RuntimeSpecError("shared memory size out of range")
        if self.stream_port is not None and not 1 <= self.stream_port <= 65535:
            raise RuntimeSpecError("stream port out of range")
        mount = self.mount_path
        if (
            not mount.startswith("/")
            or mount == "/"
            or ".." in mount.split("/")
            or any(c in mount for c in ',:"\x00\n')
        ):
            raise RuntimeSpecError("invalid mount path")
        if self.data_dir is not None:
            data = self.data_dir
            # The path is embedded in a comma-separated --mount value; commas/quotes would let a
            # path smuggle extra mount options, so they are rejected outright.
            if any(c in data for c in ',"\x00\n') or (
                not PurePath(data).is_absolute() and not re.match(r"^[A-Za-z]:[\\/]", data)
            ):
                raise RuntimeSpecError(
                    "data directory path is not usable (must be absolute, no commas or quotes)"
                )
        for key, value in self.labels.items():
            if not re.match(r"^orbitnest\.[a-z0-9.]{1,40}$", key) or not re.match(
                r"^[A-Za-z0-9._-]{0,80}$", value
            ):
                raise RuntimeSpecError("invalid label")


@dataclass(frozen=True)
class RuntimeState:
    state: RuntimeStateName
    healthy: bool | None = None  # None: no health check defined or not yet known
    exit_code: int | None = None
    detail: str | None = None


class RuntimeAdapter(ABC):
    name: str
    isolation: Literal["container", "none"]

    @abstractmethod
    def create(self, spec: RuntimeSpec) -> str:
        """Create (but do not start) a runtime resource and return its opaque id."""

    @abstractmethod
    def start(self, resource_id: str) -> None: ...

    @abstractmethod
    def stop(self, resource_id: str, grace_seconds: int) -> None:
        """Ask the workload to stop, escalate to a forced kill after ``grace_seconds``.

        On return the workload is not running, or RuntimeFailure is raised.
        """

    @abstractmethod
    def remove(self, resource_id: str) -> None:
        """Remove the resource (stopping it first if needed). Missing resources are not an error."""

    @abstractmethod
    def inspect(self, resource_id: str) -> RuntimeState: ...

    def open_shell(self, resource_id: str, cols: int, rows: int) -> ShellSession:
        """Open an interactive shell inside the workspace. Adapters without support raise."""
        raise RuntimeFailure("Terminals are not supported by this runtime.", operation="shell")

    def has_image(self, image: str) -> bool | None:
        """True/False if the image is present locally; None when this adapter cannot tell."""
        return None

    def stream_bridge_argv(self, resource_id: str, port: int) -> tuple[list[str], dict[str, str]]:
        """Command (and environment) whose stdin/stdout is a byte pipe to ``127.0.0.1:port`` inside
        the workspace. Nothing is published on the host."""
        raise RuntimeFailure("Streaming is not supported by this runtime.", operation="stream")

    def asset_argv(self, resource_id: str, path: str) -> tuple[list[str], dict[str, str]]:
        """Command that writes one viewer asset (path already validated) to stdout."""
        raise RuntimeFailure("Streaming is not supported by this runtime.", operation="stream")

    @abstractmethod
    def list_managed(self) -> dict[str, dict[str, str]]:
        """Every resource this adapter manages: id -> labels. Used to find orphans."""
