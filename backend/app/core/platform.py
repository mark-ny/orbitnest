"""Platform and runtime capability detection.

All inputs (environment, platform strings, command runner, ``which``) are injectable so the
Termux / Windows / WSL branches can be tested by simulation on any machine. Simulated tests prove
the *decision logic*, not that the real platform behaves that way; see BUILD_STATUS.md.

Commands are executed as argv lists with a timeout. Never with a shell.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

OsFamily = Literal["linux", "windows", "macos", "android-termux", "other"]
Mode = Literal[
    "linux-docker",
    "linux-no-container",
    "windows-docker-desktop",
    "windows-no-container",
    "termux-local",
    "other-no-container",
]
FeatureState = Literal["available", "limited", "remote_required", "unsupported", "not_implemented"]

COMMAND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], CommandResult]
Which = Callable[[str], str | None]


def run_command(argv: Sequence[str], timeout: float = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    """Run argv (no shell). Failures are returned as non-zero results, never raised."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, fixed commands, no shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(127, "", "command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "timed out")
    except OSError as exc:
        return CommandResult(126, "", exc.strerror or "os error")
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


class PlatformInfo(BaseModel):
    os_family: OsFamily
    os_release: str
    architecture: str
    python_version: str
    is_termux: bool
    is_wsl: bool


class RuntimeStatus(BaseModel):
    name: str
    available: bool
    version: str | None = None
    detail: str
    remediation: str | None = None


class StorageStatus(BaseModel):
    writable: bool
    free_bytes: int | None
    detail: str


class FeatureStatus(BaseModel):
    state: FeatureState
    reason: str
    remediation: str | None = None


class Capabilities(BaseModel):
    mode: Mode
    platform: PlatformInfo
    runtimes: list[RuntimeStatus]
    storage: StorageStatus
    features: dict[str, FeatureStatus]


def detect_platform(
    environ: Mapping[str, str] | None = None,
    *,
    sys_platform: str | None = None,
    machine: str | None = None,
    release: str | None = None,
) -> PlatformInfo:
    env = os.environ if environ is None else environ
    plat = sys_platform or sys.platform
    is_termux = bool(env.get("TERMUX_VERSION")) or "com.termux" in env.get("PREFIX", "")
    rel = release if release is not None else _platform.release()
    is_wsl = plat.startswith("linux") and (bool(env.get("WSL_DISTRO_NAME")) or "microsoft" in rel.lower())
    family: OsFamily
    if is_termux:
        family = "android-termux"
    elif plat.startswith("win"):
        family = "windows"
    elif plat == "darwin":
        family = "macos"
    elif plat.startswith("linux"):
        family = "linux"
    else:
        family = "other"
    return PlatformInfo(
        os_family=family,
        os_release=rel,
        architecture=(machine or _platform.machine() or "unknown").lower(),
        python_version=_platform.python_version(),
        is_termux=is_termux,
        is_wsl=is_wsl,
    )


def _docker_remediation(info: PlatformInfo) -> str:
    if info.is_termux:
        return (
            "Docker Engine does not run in ordinary Termux. Use a remote Docker-capable Linux "
            "host and connect to it over HTTPS."
        )
    if info.os_family == "windows":
        return (
            "Install and start Docker Desktop with the WSL 2 backend "
            "(run `wsl --status` in PowerShell to check WSL 2)."
        )
    return (
        "Install Docker Engine and start it (e.g. `sudo systemctl start docker`). "
        "Note: membership in the docker group is root-equivalent on the host."
    )


def detect_docker(info: PlatformInfo, which: Which, runner: Runner) -> list[RuntimeStatus]:
    cli = which("docker")
    if not cli:
        return [
            RuntimeStatus(
                name="docker",
                available=False,
                detail="docker CLI not found",
                remediation=_docker_remediation(info),
            )
        ]
    probe = runner([cli, "version", "--format", "{{.Server.Version}}"], COMMAND_TIMEOUT_SECONDS)
    if probe.returncode != 0 or not probe.stdout:
        return [
            RuntimeStatus(
                name="docker",
                available=False,
                detail="docker CLI found but the daemon is not reachable",
                remediation=_docker_remediation(info),
            )
        ]
    statuses = [
        RuntimeStatus(name="docker", available=True, version=probe.stdout, detail="Docker daemon reachable")
    ]
    compose = runner([cli, "compose", "version", "--short"], COMMAND_TIMEOUT_SECONDS)
    if compose.returncode == 0 and compose.stdout:
        statuses.append(
            RuntimeStatus(
                name="docker-compose",
                available=True,
                version=compose.stdout,
                detail="Docker Compose plugin present",
            )
        )
    else:
        statuses.append(
            RuntimeStatus(
                name="docker-compose",
                available=False,
                detail="Docker Compose plugin not found",
                remediation="Install the Docker Compose v2 plugin.",
            )
        )
    return statuses


def detect_proot(info: PlatformInfo, which: Which) -> RuntimeStatus:
    if which("proot-distro"):
        return RuntimeStatus(
            name="proot-distro",
            available=True,
            detail="proot-distro present. PRoot userlands are NOT a security boundary.",
        )
    return RuntimeStatus(
        name="proot-distro",
        available=False,
        detail="proot-distro not installed",
        remediation="Termux only: `pkg install proot-distro` (optional; provides no isolation).",
    )


def detect_storage(data_dir: Path) -> StorageStatus:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir):
            pass
        free = shutil.disk_usage(data_dir).free
        return StorageStatus(writable=True, free_bytes=free, detail="Data directory is writable")
    except OSError as exc:
        return StorageStatus(
            writable=False,
            free_bytes=None,
            detail=f"Data directory is not writable ({exc.strerror or 'os error'})",
        )


def _determine_mode(info: PlatformInfo, docker_ok: bool) -> Mode:
    if info.os_family == "android-termux":
        return "termux-local"
    if info.os_family == "windows":
        return "windows-docker-desktop" if docker_ok else "windows-no-container"
    if info.os_family == "linux":
        return "linux-docker" if docker_ok else "linux-no-container"
    return "other-no-container"


def _features(mode: Mode, info: PlatformInfo, proot: RuntimeStatus | None) -> dict[str, FeatureStatus]:
    container_ok = mode in {"linux-docker", "windows-docker-desktop"}
    remote_hint = "Run workspaces on a remote Docker-capable Linux host and use this device as the client."
    features: dict[str, FeatureStatus] = {}
    if container_ok:
        features["container_workspaces"] = FeatureStatus(
            state="available", reason="A container runtime is reachable."
        )
    elif mode == "termux-local":
        features["container_workspaces"] = FeatureStatus(
            state="remote_required",
            reason="Docker Engine is not available in ordinary Termux.",
            remediation=remote_hint,
        )
    else:
        features["container_workspaces"] = FeatureStatus(
            state="unsupported",
            reason="No reachable container runtime was detected.",
            remediation=_docker_remediation(info),
        )
    if mode == "termux-local":
        features["local_process_workspaces"] = FeatureStatus(
            state="limited",
            reason="Local folders and processes only. No sandbox isolation.",
        )
        features["proot_userland"] = FeatureStatus(
            state="limited" if proot and proot.available else "unsupported",
            reason=(
                "proot-distro present; PRoot is not equivalent to container isolation."
                if proot and proot.available
                else "proot-distro is not installed."
            ),
            remediation=None if proot and proot.available else proot.remediation if proot else None,
        )
    else:
        features["local_process_workspaces"] = FeatureStatus(
            state="unsupported", reason="Local-process workspaces are only offered in Termux mode."
        )
        features["proot_userland"] = FeatureStatus(
            state="unsupported", reason="PRoot userlands are only offered in Termux mode."
        )
    if mode in ("linux-docker", "windows-docker-desktop"):
        features["browser_terminal"] = FeatureStatus(
            state="available",
            reason="Shell inside the workspace container (docker exec). Needs WebSocket support (the "
            "'websockets' package) in the server environment.",
        )
    elif mode == "termux-local":
        features["browser_terminal"] = FeatureStatus(
            state="limited",
            reason="A shell on this device as your own user, without isolation. Administrators only.",
        )
    else:
        features["browser_terminal"] = FeatureStatus(
            state="unsupported", reason="Terminals need a workspace runtime, and none is available."
        )
    if info.os_family == "windows":
        features["file_manager"] = FeatureStatus(
            state="unsupported",
            reason="The symlink-safe file manager needs a POSIX host.",
            remediation="Run the OrbitNest control plane inside WSL 2 instead of native Windows Python.",
        )
    else:
        features["file_manager"] = FeatureStatus(
            state="available", reason="Files are managed on the host and work while a workspace is stopped."
        )
    # Browser and desktop workspaces are streamed from a container (VNC bridged over `docker exec`),
    # so they need exactly what container workspaces need. Whether a particular template's image is
    # built is reported per template, not here.
    if mode in ("linux-docker", "windows-docker-desktop"):
        features["desktop_streaming"] = FeatureStatus(
            state="available",
            reason="Browser and desktop workspaces are streamed from containers. Each template's image "
            "must be built first (Admin, Templates).",
        )
    else:
        features["desktop_streaming"] = FeatureStatus(
            state="unsupported",
            reason="Streaming browsers and desktops need a container runtime, which this host does not have.",
            remediation=_docker_remediation(info),
        )
    return features


def detect_capabilities(
    data_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    sys_platform: str | None = None,
    machine: str | None = None,
    release: str | None = None,
    which: Which = shutil.which,
    runner: Runner = run_command,
) -> Capabilities:
    info = detect_platform(environ, sys_platform=sys_platform, machine=machine, release=release)
    runtimes: list[RuntimeStatus] = []
    docker_ok = False
    proot: RuntimeStatus | None = None
    if info.is_termux:
        # Never probe for a Docker daemon in Termux: report it as unavailable by design.
        runtimes.append(
            RuntimeStatus(
                name="docker",
                available=False,
                detail="Not supported in ordinary Termux",
                remediation=_docker_remediation(info),
            )
        )
        proot = detect_proot(info, which)
        runtimes.append(proot)
    else:
        docker = detect_docker(info, which, runner)
        runtimes.extend(docker)
        docker_ok = docker[0].available
    mode = _determine_mode(info, docker_ok)
    return Capabilities(
        mode=mode,
        platform=info,
        runtimes=runtimes,
        storage=detect_storage(data_dir),
        features=_features(mode, info, proot),
    )
