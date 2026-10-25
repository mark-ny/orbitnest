"""Local-process runtime for Termux (and any POSIX host without a container runtime).

THERE IS NO ISOLATION HERE. A "workspace" is a project folder plus, optionally, one supervised
process started from an allowlisted executable. The process runs as the same OS user as the
control plane. This adapter exists so Termux users can manage local project folders; it must never
be presented as a sandbox. (Docker does not run in ordinary Termux; PRoot is not a security
boundary either.)

What it does enforce, because it is cheap and portable:

* the child gets a minimal environment (never the control plane's environment, which holds
  secrets), its own session/process group, stdin from /dev/null and a log file
* RLIMIT_FSIZE (largest single file) and RLIMIT_CORE=0, applied by a tiny bootstrap that then
  ``exec``s the real program (no ``preexec_fn``, which is unsafe in multithreaded programs)
* termination by process group, SIGTERM then SIGKILL after a bounded grace period

What it does NOT do: CPU, memory or PID limits (``RLIMIT_AS``/``RLIMIT_NPROC`` are unreliable for
Node/V8 and count per-user processes), filesystem confinement, network isolation.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.runtimes.base import RuntimeAdapter, RuntimeFailure, RuntimeSpec, RuntimeState
from app.runtimes.pty_shell import PtyShell, ShellSession, local_shell_argv

# Executables a local template may start. Deliberately tiny; shells and package managers excluded.
ALLOWED_EXECUTABLES = frozenset({"python3", "python", "node"})
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")

_BOOTSTRAP = (
    "import os,resource,sys\n"
    "fs=int(sys.argv[1])*1048576\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(fs,fs))\n"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
    "os.execvp(sys.argv[2],sys.argv[2:])\n"
)


def _proc_start_ticks(pid: int) -> str | None:
    """Process start time from /proc (guards against PID reuse). None if /proc is unavailable."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        # Field 2 (comm) may contain spaces/parens; split after the last ')'.
        return text.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _proc_state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


@dataclass
class LocalProcessRuntime(RuntimeAdapter):
    state_dir: Path
    name: str = "local"
    isolation: str = "none"
    _children: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if sys.platform.startswith("win"):
            raise RuntimeFailure("the local-process runtime is POSIX only")
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.state_dir.chmod(0o700)

    # -- state files ---------------------------------------------------------------------
    def _path(self, rid: str) -> Path:
        if not rid.startswith("local-") or not rid[6:].replace("-", "").isalnum():
            raise RuntimeFailure("invalid local resource id")
        return self.state_dir / f"{rid}.json"

    def _load(self, rid: str) -> dict | None:
        try:
            return json.loads(self._path(rid).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _save(self, rid: str, data: dict) -> None:
        tmp = self._path(rid).with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._path(rid))

    # -- liveness ------------------------------------------------------------------------
    def _alive(self, rid: str, data: dict) -> bool:
        pid = data.get("pid")
        if not pid:
            return False
        child = self._children.get(rid)
        if child is not None:
            return child.poll() is None
        if _proc_state(pid) == "Z":
            return False
        ticks = data.get("start_ticks")
        if ticks is not None:
            return _proc_start_ticks(pid) == ticks
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    # -- adapter API ---------------------------------------------------------------------
    def create(self, spec: RuntimeSpec) -> str:
        spec.validate()
        if spec.data_dir is None or not Path(spec.data_dir).is_dir():
            raise RuntimeFailure("workspace folder does not exist")
        if spec.local_command and Path(spec.local_command[0]).name not in ALLOWED_EXECUTABLES:
            raise RuntimeFailure("executable is not allowed for local workspaces")
        rid = f"local-{spec.workspace_id.lower()}-{int(time.time() * 1000):x}"
        self._save(
            rid,
            {
                "workspace_id": spec.workspace_id,
                "data_dir": spec.data_dir,
                "argv": list(spec.local_command),
                "max_file_mb": spec.max_file_mb,
                "running": False,
                "started_once": False,
                "pid": None,
                "start_ticks": None,
                "labels": dict(spec.labels),
            },
        )
        return rid

    def _env(self, data_dir: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
        env["HOME"] = data_dir
        return env

    def start(self, resource_id: str) -> None:
        data = self._load(resource_id)
        if data is None:
            raise RuntimeFailure("resource not found", operation="start")
        if data["running"] and (not data["argv"] or self._alive(resource_id, data)):
            return
        if not data["argv"]:
            data.update(running=True, started_once=True)  # folder-only workspace: "running" means open
            self._save(resource_id, data)
            return
        log_path = self.state_dir / f"{resource_id}.log"
        try:
            with open(log_path, "ab") as log:
                child = subprocess.Popen(  # noqa: S603 - allowlisted executable, argv list
                    [sys.executable, "-c", _BOOTSTRAP, str(data["max_file_mb"]), *data["argv"]],
                    cwd=data["data_dir"],
                    env=self._env(data["data_dir"]),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            raise RuntimeFailure(
                f"could not start process: {exc.strerror or 'os error'}", operation="start"
            ) from None
        self._children[resource_id] = child
        data.update(running=True, started_once=True, pid=child.pid, start_ticks=_proc_start_ticks(child.pid))
        self._save(resource_id, data)

    def stop(self, resource_id: str, grace_seconds: int) -> None:
        data = self._load(resource_id)
        if data is None:
            return
        pid = data.get("pid")
        if pid and self._alive(resource_id, data):
            self._signal_group(pid, signal.SIGTERM)
            deadline = time.monotonic() + max(0.1, grace_seconds)
            while time.monotonic() < deadline and self._alive(resource_id, data):
                time.sleep(0.05)
            if self._alive(resource_id, data):
                self._signal_group(pid, signal.SIGKILL)
                kill_deadline = time.monotonic() + 5
                while time.monotonic() < kill_deadline and self._alive(resource_id, data):
                    time.sleep(0.05)
                if self._alive(resource_id, data):
                    raise RuntimeFailure("process did not stop", operation="stop")
        child = self._children.pop(resource_id, None)
        if child is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=1)
        data.update(running=False, pid=None, start_ticks=None)
        self._save(resource_id, data)

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), sig)

    def remove(self, resource_id: str) -> None:
        if self._load(resource_id) is not None:
            self.stop(resource_id, 2)
        for suffix in (".json", ".log", ".tmp"):
            with contextlib.suppress(OSError):
                (self.state_dir / f"{resource_id}{suffix}").unlink()

    def inspect(self, resource_id: str) -> RuntimeState:
        data = self._load(resource_id)
        if data is None:
            return RuntimeState("missing")
        if not data["running"]:
            if data.get("started_once"):
                return RuntimeState("exited", exit_code=0)
            return RuntimeState("created")
        if not data["argv"] or self._alive(resource_id, data):
            return RuntimeState("running")
        child = self._children.get(resource_id)
        return RuntimeState("exited", exit_code=child.returncode if child else None, detail="process exited")

    def open_shell(self, resource_id: str, cols: int, rows: int) -> ShellSession:
        """A shell in the workspace folder as the control plane's own OS user: NO isolation.
        Callers must restrict this to administrators (see TerminalService)."""
        data = self._load(resource_id)
        if data is None or not data["running"]:
            raise RuntimeFailure("workspace is not running", operation="shell")
        shell = next((p for p in (shutil.which("bash"), shutil.which("sh")) if p), None)
        if shell is None:
            raise RuntimeFailure("no shell is installed", operation="shell")
        env = self._env(data["data_dir"])
        env["TERM"] = "xterm-256color"
        env["SHELL"] = shell
        try:
            return PtyShell.spawn(
                local_shell_argv(int(data["max_file_mb"]), shell),
                env=env,
                cwd=data["data_dir"],
                cols=cols,
                rows=rows,
            )
        except OSError as exc:
            raise RuntimeFailure(
                f"could not open a terminal: {exc.strerror or 'os error'}", operation="shell"
            ) from None

    def list_managed(self) -> dict[str, dict[str, str]]:
        found = {}
        for path in self.state_dir.glob("local-*.json"):
            data = self._load(path.stem)
            if data is not None:
                found[path.stem] = dict(data.get("labels", {}))
        return found
