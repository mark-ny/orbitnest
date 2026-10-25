"""Real-process tests for the Termux-mode local runtime (runs on any POSIX host)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.runtimes.base import RuntimeFailure
from app.runtimes.termux_runtime import LocalProcessRuntime
from tests.runtime_helpers import make_spec

PY = "python3"


@unittest.skipIf(sys.platform.startswith("win"), "POSIX only")
class LocalProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.rt = LocalProcessRuntime(self.tmp / "state")
        self.rids: list[str] = []

    def tearDown(self) -> None:
        for rid in self.rids:
            self.rt.remove(rid)
        self._tmp.cleanup()

    def make(self, code: str, **kw) -> str:
        rid = self.rt.create(make_spec(str(self.data), local_command=(PY, "-c", code), **kw))
        self.rids.append(rid)
        return rid

    def wait_for(self, predicate, timeout=5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_process_runs_in_workspace_dir_and_stops_on_sigterm(self) -> None:
        rid = self.make("import os,time;open('cwd.txt','w').write(os.getcwd());time.sleep(60)")
        self.rt.start(rid)
        self.assertTrue(self.wait_for(lambda: (self.data / "cwd.txt").exists()))
        self.assertEqual(Path((self.data / "cwd.txt").read_text()).resolve(), self.data.resolve())
        self.assertEqual(self.rt.inspect(rid).state, "running")
        started = time.monotonic()
        self.rt.stop(rid, 5)
        self.assertLess(time.monotonic() - started, 3, "SIGTERM should end a cooperative process promptly")
        self.assertEqual(self.rt.inspect(rid).state, "exited")

    def test_sigterm_ignoring_process_is_force_killed_after_grace(self) -> None:
        rid = self.make(
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "open('ready','w').write('1');time.sleep(60)"
        )
        self.rt.start(rid)
        self.assertTrue(self.wait_for(lambda: (self.data / "ready").exists()))
        started = time.monotonic()
        self.rt.stop(rid, 1)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.9, "should wait the grace period first")
        self.assertLess(elapsed, 4)
        self.assertEqual(self.rt.inspect(rid).state, "exited")

    def test_whole_process_group_is_terminated(self) -> None:
        code = (
            "import subprocess,time;"
            "p=subprocess.Popen(['sleep','60']);open('child.pid','w').write(str(p.pid));time.sleep(60)"
        )
        rid = self.make(code)
        self.rt.start(rid)
        self.assertTrue(self.wait_for(lambda: (self.data / "child.pid").exists()))
        child_pid = int((self.data / "child.pid").read_text())
        self.rt.stop(rid, 2)

        def gone() -> bool:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return True
            try:  # a zombie still answers kill(0); consult /proc
                return Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
            except OSError:
                return True

        self.assertTrue(self.wait_for(gone), "grandchild must not outlive the workspace")

    def test_child_does_not_inherit_control_plane_secrets(self) -> None:
        with mock.patch.dict(
            os.environ, {"ORBITNEST_SECRET_KEY": "super-secret-value-xyz", "AWS_SECRET_ACCESS_KEY": "abc"}
        ):
            rid = self.make("import os,json;open('env.json','w').write(json.dumps(dict(os.environ)))")
            self.rt.start(rid)
        self.assertTrue(self.wait_for(lambda: (self.data / "env.json").exists()))
        time.sleep(0.2)
        env = json.loads((self.data / "env.json").read_text())
        self.assertNotIn("ORBITNEST_SECRET_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["HOME"], str(self.data))

    def test_file_size_limit_enforced(self) -> None:
        code = (
            "try:\n open('big','wb').write(b'x'*(3*1024*1024));r='wrote'\n"
            "except OSError:\n r='blocked'\nopen('result','w').write(r)"
        )
        rid = self.make(code.replace("\n", "\n"), max_file_mb=1)
        self.rt.start(rid)
        self.assertTrue(self.wait_for(lambda: (self.data / "result").exists()))
        self.assertEqual((self.data / "result").read_text(), "blocked")

    def test_process_exit_detected(self) -> None:
        rid = self.make("pass")
        self.rt.start(rid)
        self.assertTrue(self.wait_for(lambda: self.rt.inspect(rid).state == "exited"))

    def test_survives_control_plane_restart(self) -> None:
        rid = self.make("import time;time.sleep(60)")
        self.rt.start(rid)
        restarted = LocalProcessRuntime(self.tmp / "state")  # new instance: no Popen handles
        self.assertEqual(restarted.inspect(rid).state, "running")
        restarted.stop(rid, 3)
        self.assertEqual(restarted.inspect(rid).state, "exited")

    def test_pid_reuse_guard(self) -> None:
        rid = self.make("import time;time.sleep(60)")
        self.rt.start(rid)
        restarted = LocalProcessRuntime(self.tmp / "state")
        path = self.tmp / "state" / f"{rid}.json"
        data = json.loads(path.read_text())
        if data["start_ticks"] is None:
            self.skipTest("/proc unavailable")
        data["start_ticks"] = "1"  # pretend the pid now belongs to a different process
        path.write_text(json.dumps(data))
        self.assertEqual(restarted.inspect(rid).state, "exited")

    def test_disallowed_executables_rejected(self) -> None:
        for exe in ("bash", "sh", "/bin/sh", "rm", "curl"):
            with self.subTest(exe=exe), self.assertRaises(RuntimeFailure):
                self.rt.create(make_spec(str(self.data), local_command=(exe, "-c", "true")))

    def test_missing_workspace_folder_rejected(self) -> None:
        with self.assertRaises(RuntimeFailure):
            self.rt.create(make_spec(str(self.tmp / "nope")))

    def test_resource_ids_cannot_escape_state_dir(self) -> None:
        for bad in ("../x", "local-../../etc/passwd", "x", "local-a/b"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeFailure):
                self.rt.inspect(bad)

    def test_folder_only_workspace_has_no_process(self) -> None:
        rid = self.rt.create(make_spec(str(self.data)))
        self.rids.append(rid)
        self.rt.start(rid)
        self.assertEqual(self.rt.inspect(rid).state, "running")
        self.rt.stop(rid, 1)
        self.assertEqual(self.rt.inspect(rid).state, "exited")

    def test_state_dir_private(self) -> None:
        self.assertEqual((self.tmp / "state").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
