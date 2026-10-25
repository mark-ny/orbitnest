"""Interactive shell sessions over a pseudo-terminal.

``PtyShell`` spawns an argv list (never a shell string) with the pty slave as stdin/stdout/stderr
in its own session, and exposes a small thread-friendly API. It is used for ``docker exec -it`` and
for the local runtime's shell. POSIX only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

MIN_COLS, MAX_COLS = 20, 500
MIN_ROWS, MAX_ROWS = 5, 200

# Local shells: make the pty the controlling terminal (job control works), cap file size/core
# dumps, then exec. A bootstrap instead of preexec_fn, which is unsafe in multithreaded programs.
LOCAL_SHELL_BOOTSTRAP = (
    "import fcntl,os,resource,sys,termios\n"
    "try:\n"
    "    fcntl.ioctl(0,termios.TIOCSCTTY,0)\n"
    "except OSError:\n"
    "    pass\n"
    "fs=int(sys.argv[1])*1048576\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(fs,fs))\n"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
    "os.execvp(sys.argv[2],sys.argv[2:])\n"
)


def clamp_size(cols: int, rows: int) -> tuple[int, int]:
    return max(MIN_COLS, min(int(cols), MAX_COLS)), max(MIN_ROWS, min(int(rows), MAX_ROWS))


class ShellSession(ABC):
    """One interactive terminal. ``read`` may block for up to ``timeout`` seconds."""

    @abstractmethod
    def read(self, timeout: float) -> bytes | None:
        """Bytes of output, ``None`` when nothing arrived in time, ``b""`` once the shell has ended."""

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None: ...

    @abstractmethod
    def close(self) -> None:
        """Terminate the shell and release resources. Idempotent."""

    @property
    @abstractmethod
    def exit_code(self) -> int | None: ...


class PtyShell(ShellSession):
    def __init__(self, proc: subprocess.Popen[bytes], master_fd: int) -> None:
        self._proc = proc
        self._fd = master_fd
        self._closed = False

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None,
        cols: int,
        rows: int,
    ) -> PtyShell:
        cols, rows = clamp_size(cols, rows)
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            proc = subprocess.Popen(  # noqa: S603 - argv list built by adapters, never from user text
                list(argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=dict(env),
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        return cls(proc, master)

    # -- io ------------------------------------------------------------------------------
    def read(self, timeout: float) -> bytes | None:
        if self._closed:
            return b""
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b"" if self._proc.poll() is not None and not self._drain_possible() else None
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return None
        except OSError as exc:  # EIO: every slave end closed => the shell has exited
            if exc.errno == errno.EIO:
                return b""
            raise
        return data or b""

    def _drain_possible(self) -> bool:
        try:
            ready, _, _ = select.select([self._fd], [], [], 0)
        except (OSError, ValueError):
            return False
        return bool(ready)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + 5
        while view and not self._closed:
            try:
                sent = os.write(self._fd, view)
                view = view[sent:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return  # the shell is not reading; drop rather than stall the caller
                select.select([], [self._fd], [], 0.1)
            except OSError:
                return

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = clamp_size(cols, rows)
        if self._closed:
            return
        with contextlib.suppress(OSError):
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self._signal(signal.SIGWINCH)  # e.g. the docker CLI has no controlling tty to learn it from

    def _signal(self, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self._proc.pid, sig)

    @property
    def exit_code(self) -> int | None:
        return self._proc.poll()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._signal(signal.SIGHUP)
        try:
            self._proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._signal(signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=2.0)
        with contextlib.suppress(OSError):
            os.close(self._fd)


def local_shell_argv(max_file_mb: int, shell: str) -> list[str]:
    return [sys.executable, "-c", LOCAL_SHELL_BOOTSTRAP, str(max_file_mb), shell, "-i"]
