"""Docker Engine / Docker Desktop adapter (Linux, and Windows via Docker Desktop + WSL 2).

STATUS: verified against the installed Docker CLI's flag names (``--help``) and against a scripted
fake CLI; NOT verified against a real Docker daemon in the build environment (none was reachable).
Treat first use on a real host as a test.

Security posture (all hard-coded; nothing here is caller-configurable):

* argv lists only, never a shell; resource ids are validated before being passed to docker
* ``--cap-drop ALL`` (plus allowlisted adds), ``no-new-privileges``, default seccomp/AppArmor kept
* never ``--privileged``, never host network/PID/IPC, never published ports, never ``--device``
* the Docker socket is never mounted; the only mount is the workspace data directory
* memory/CPU/PID limits, swap disabled, bounded log size, non-root user
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.platform import CommandResult
from app.runtimes.base import RuntimeAdapter, RuntimeFailure, RuntimeSpec, RuntimeState

NETWORK_NAME = "orbitnest-workspaces"
RESOURCE_ID_RE = re.compile(r"^[a-f0-9]{12,64}$")
LABEL_MANAGED = "orbitnest.managed"
LABEL_WORKSPACE = "orbitnest.workspace"

CommandRunner = Callable[[Sequence[str], float], CommandResult]

CREATE_TIMEOUT = 900.0  # may include an image pull
DEFAULT_TIMEOUT = 30.0


def subprocess_runner(argv: Sequence[str], timeout: float) -> CommandResult:
    try:
        proc = subprocess.run(  # noqa: S603 - argv list; first element is the docker CLI
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return CommandResult(127, "", "docker CLI not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "timed out")
    except OSError as exc:
        return CommandResult(126, "", exc.strerror or "os error")
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def container_user() -> str:
    """Run as a non-root user that can write the bind-mounted data directory.

    POSIX: the control plane's own uid:gid (so no chown is needed), or 1000:1000 if the control
    plane runs as root (the service then chowns the data directory). Windows/Docker Desktop:
    1000:1000 (Docker Desktop's file sharing handles bind-mount permissions).
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None or getuid() == 0:
        return "1000:1000"
    return f"{getuid()}:{os.getgid()}"


@dataclass
class DockerRuntime(RuntimeAdapter):
    name: str = "docker"
    isolation: str = "container"
    docker_bin: str = "docker"
    run: CommandRunner = subprocess_runner
    redact: tuple[str, ...] = ()  # host paths to scrub from error messages
    user: str | None = None

    # -- helpers -------------------------------------------------------------------------
    def _safe(self, text: str) -> str:
        """First line of docker's stderr (the error; later lines are usage hints), paths scrubbed."""
        for path in self.redact:
            if path:
                text = text.replace(path, "<data>")
        line = (text.strip().splitlines() or [""])[0]
        return re.sub(r"\s+", " ", line)[:200] or "unknown error"

    def _docker(self, args: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        return self.run([self.docker_bin, *args], timeout)

    def _check(self, result: CommandResult, op: str, *, tolerate: tuple[str, ...] = ()) -> CommandResult:
        if result.returncode == 0:
            return result
        if any(t in result.stderr.lower() for t in tolerate):
            return result
        if result.returncode == 124:
            raise RuntimeFailure(f"docker {op} timed out", operation=op)
        if result.returncode in (126, 127):
            raise RuntimeFailure("Docker CLI is not available", operation=op)
        raise RuntimeFailure(f"docker {op} failed: {self._safe(result.stderr)}", operation=op)

    @staticmethod
    def _rid(resource_id: str) -> str:
        if not RESOURCE_ID_RE.match(resource_id):
            raise RuntimeFailure("invalid container id")
        return resource_id

    # -- network -------------------------------------------------------------------------
    def ensure_network(self) -> None:
        if self._docker(["network", "inspect", NETWORK_NAME]).returncode == 0:
            return
        self._check(
            self._docker(
                [
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--opt",
                    "com.docker.network.bridge.enable_icc=false",
                    "--label",
                    f"{LABEL_MANAGED}=true",
                    NETWORK_NAME,
                ]
            ),
            "network create",
            tolerate=("already exists",),
        )

    # -- argv construction (pure; unit-tested) ---------------------------------------------
    def build_create_argv(self, spec: RuntimeSpec, *, grace_seconds: int = 10) -> list[str]:
        spec.validate()
        if spec.data_dir is None:
            raise RuntimeFailure("a data directory is required")
        name = f"orbitnest-ws-{spec.workspace_id.lower()}"
        network = "none" if spec.network == "none" else NETWORK_NAME
        argv = [
            self.docker_bin,
            "create",
            "--name",
            name,
            "--label",
            f"{LABEL_MANAGED}=true",
            "--label",
            f"{LABEL_WORKSPACE}={spec.workspace_id}",
            "--init",
            "--user",
            self.user or container_user(),
            "--cap-drop",
            "ALL",
        ]
        for cap in spec.capabilities:
            argv += ["--cap-add", cap]
        argv += [
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(spec.pids),
            "--memory",
            f"{spec.memory_mb}m",
            "--memory-swap",
            f"{spec.memory_mb}m",
            "--cpus",
            f"{spec.cpu_millis / 1000:.2f}",
            "--ulimit",
            "core=0",
            "--network",
            network,
            "--ipc",
            "private",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",  # noqa: S108 - container-internal tmpfs
            "--mount",
            f"type=bind,source={spec.data_dir},target={spec.mount_path}",
            "--workdir",
            spec.mount_path,
            "--env",
            f"HOME={spec.mount_path}",
            "--restart",
            "no",
            "--stop-timeout",
            str(grace_seconds),
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=2",
        ]
        if spec.healthcheck:
            argv += [
                "--health-cmd",
                " ".join(spec.healthcheck),
                "--health-interval",
                "30s",
                "--health-retries",
                "3",
            ]
        argv.append(spec.image)
        argv += list(spec.command)
        return argv

    # -- adapter API ---------------------------------------------------------------------
    def create(self, spec: RuntimeSpec) -> str:
        argv = self.build_create_argv(spec)  # validates first: invalid specs never reach docker
        if spec.network != "none":
            self.ensure_network()
        result = self._check(self.run(argv, CREATE_TIMEOUT), "create")
        container_id = (result.stdout.strip().splitlines() or [""])[-1]
        if not RESOURCE_ID_RE.match(container_id):
            raise RuntimeFailure("docker create returned an unexpected result", operation="create")
        return container_id

    def start(self, resource_id: str) -> None:
        self._check(self._docker(["start", self._rid(resource_id)], timeout=60), "start")

    def stop(self, resource_id: str, grace_seconds: int) -> None:
        rid = self._rid(resource_id)
        grace = max(1, min(int(grace_seconds), 120))
        # `docker stop -t N` sends SIGTERM then SIGKILL after N seconds.
        self._check(
            self._docker(["stop", "-t", str(grace), rid], timeout=grace + 30),
            "stop",
            tolerate=("no such container",),
        )
        state = self.inspect(rid)
        if state.state == "running":
            # Belt and braces: the bounded stop did not take effect, force termination.
            self._check(self._docker(["kill", rid]), "kill", tolerate=("is not running", "no such container"))
            if self.inspect(rid).state == "running":
                raise RuntimeFailure("container did not stop", operation="stop")

    def remove(self, resource_id: str) -> None:
        self._check(
            self._docker(["rm", "-f", self._rid(resource_id)], timeout=60),
            "remove",
            tolerate=("no such container",),
        )

    def inspect(self, resource_id: str) -> RuntimeState:
        result = self._docker(["inspect", "--format", "{{json .State}}", self._rid(resource_id)])
        if result.returncode != 0:
            if "no such" in result.stderr.lower():
                return RuntimeState("missing")
            return RuntimeState("unknown", detail=self._safe(result.stderr))
        try:
            state = json.loads(result.stdout)
        except json.JSONDecodeError:
            return RuntimeState("unknown", detail="unreadable inspect output")
        status = state.get("Status")
        health = (state.get("Health") or {}).get("Status")
        healthy = None if health in (None, "", "starting") else health == "healthy"
        if status == "running":
            name = "running"
        elif status == "created":
            name = "created"
        elif status in {"exited", "dead"}:
            name = "exited"
        else:
            name = "unknown"
        return RuntimeState(name, healthy, state.get("ExitCode"), (state.get("Error") or None))  # type: ignore[arg-type]

    def list_managed(self) -> dict[str, dict[str, str]]:
        result = self._check(
            self._docker(
                [
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    f"label={LABEL_MANAGED}=true",
                    "--format",
                    "{{json .}}",
                ]
            ),
            "list",
        )
        found: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = str(item.get("ID", ""))
            if RESOURCE_ID_RE.match(rid):
                labels = dict(p.split("=", 1) for p in str(item.get("Labels", "")).split(",") if "=" in p)
                found[rid] = labels
        return found
