"""Real pseudo-terminals and real processes (POSIX)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from app.runtimes.base import RuntimeFailure
from app.runtimes.docker_runtime import DockerRuntime
from app.runtimes.pty_shell import PtyShell, clamp_size
from app.runtimes.termux_runtime import LocalProcessRuntime
from tests.runtime_helpers import make_spec


def read_until(shell, needle: bytes, timeout: float = 10.0) -> bytes:
    buf = b""
    end = time.monotonic() + timeout
    while needle not in buf and time.monotonic() < end:
        chunk = shell.read(0.2)
        if chunk == b"":
            break
        if chunk:
            buf += chunk
    return buf


class PtyShellTests(unittest.TestCase):
    def spawn(self, argv, **kw):
        s = PtyShell.spawn(
            argv, env={"PATH": os.environ["PATH"], "TERM": "xterm"}, cwd=None, cols=80, rows=24, **kw
        )
        self.addCleanup(s.close)
        return s

    def test_echo_and_exit_code(self) -> None:
        s = self.spawn(["sh", "-c", "echo ready; read x; echo got:$x; exit 7"])
        self.assertIn(b"ready", read_until(s, b"ready"))
        s.write(b"abc\n")
        self.assertIn(b"got:abc", read_until(s, b"got:abc"))
        end = time.monotonic() + 5
        while s.exit_code is None and time.monotonic() < end:
            s.read(0.1)
        self.assertEqual(s.exit_code, 7)

    def test_is_a_real_tty_and_resize_works(self) -> None:
        s = self.spawn(["sh", "-c", "test -t 0 && echo TTY; stty size; read x; stty size"])
        out = read_until(s, b"24 80")
        self.assertIn(b"TTY", out)
        s.resize(132, 43)
        s.write(b"\n")
        self.assertIn(b"43 132", read_until(s, b"43 132"))

    def test_size_is_clamped(self) -> None:
        self.assertEqual(clamp_size(0, 0), (20, 5))
        self.assertEqual(clamp_size(99999, 99999), (500, 200))

    def test_close_kills_the_whole_process_group(self) -> None:
        s = self.spawn(["sh", "-c", "sleep 300 & echo $! ; wait"])
        out = read_until(s, b"\n")
        child = int(out.split()[0])
        s.close()
        time.sleep(0.3)
        self.assertTrue(_gone(child), "background child survived close()")
        self.assertEqual(s.read(0.1), b"")
        s.close()  # idempotent

    def test_eof_after_exit(self) -> None:
        s = self.spawn(["true"])
        end = time.monotonic() + 5
        got = None
        while time.monotonic() < end:
            got = s.read(0.2)
            if got == b"":
                break
        self.assertEqual(got, b"")


def _gone(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


class LocalShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        base = Path(self._t.name)
        (base / "data").mkdir()
        self.rt = LocalProcessRuntime(base / "state")
        self.data = base / "data"
        self.addCleanup(self._t.cleanup)

    def started(self) -> str:
        spec = make_spec(data_dir=str(self.data), local_command=())
        rid = self.rt.create(spec)
        self.rt.start(rid)
        self.addCleanup(self.rt.remove, rid)
        return rid

    def test_runs_in_workspace_dir_with_scrubbed_env(self) -> None:
        os.environ["ORBITNEST_SECRET_KEY"] = "SUPER-SECRET-VALUE-123"
        self.addCleanup(os.environ.pop, "ORBITNEST_SECRET_KEY", None)
        rid = self.started()
        s = self.rt.open_shell(rid, 100, 30)
        self.addCleanup(s.close)
        s.write(b"pwd; env | grep -c SECRET; echo END_$((20+22)); exit\n")
        out = read_until(s, b"END_42").decode(errors="replace")
        # pwd's output, then grep -c's count of SECRET variables (0), then our marker
        self.assertIn(f"{self.data}\r\n0\r\nEND_42", out)
        self.assertNotIn("SUPER-SECRET", out)

    def test_refuses_when_not_running(self) -> None:
        rid = self.started()
        self.rt.stop(rid, 1)
        with self.assertRaises(RuntimeFailure):
            self.rt.open_shell(rid, 80, 24)
        with self.assertRaises(RuntimeFailure):
            self.rt.open_shell("local-nothing", 80, 24)


class DockerExecArgvTests(unittest.TestCase):
    def test_argv_is_fixed_and_validated(self) -> None:
        captured = {}

        def spawn(argv, **kw):
            captured["argv"], captured["kw"] = argv, kw
            raise OSError(2, "boom")

        rt = DockerRuntime(spawn=spawn)
        with self.assertRaises(RuntimeFailure) as cm:
            rt.open_shell("a" * 64, 100, 30)
        self.assertEqual(cm.exception.safe_message, "could not open a terminal: boom")  # strerror only
        argv = captured["argv"]
        self.assertEqual(argv[:3], ["docker", "exec", "-it"])
        self.assertEqual(argv[argv.index("--workdir") + 1], "/home/workspace")
        self.assertEqual(argv[argv.index("a" * 64) - 1], "TERM=xterm-256color")
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("-u", argv)
        self.assertNotIn("--user", argv)  # inherits the container's non-root user
        self.assertEqual(captured["kw"]["cols"], 100)
        self.assertNotIn("ORBITNEST_SECRET_KEY", captured["kw"]["env"])
        self.assertIn("could not open a terminal", cm.exception.safe_message)
        for bad in ("; rm -rf /", "--privileged", "abc", "../x"):
            with self.assertRaises(RuntimeFailure):
                rt.open_shell(bad, 80, 24)


if __name__ == "__main__":
    unittest.main()
