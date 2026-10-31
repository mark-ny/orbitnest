"""Tests for the Docker-less phone browser adapter, against a stand-in for ``proot-distro``.

The stand-in parses the same argv the adapter builds, creates the bound session folder's ``vnc.sock``
and sleeps. It proves our lifecycle, wipe and argv handling; it proves nothing about real proot,
Xvnc, Tor or Chromium, which only a phone can test.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from app.runtimes import proot_browser as pb
from app.runtimes.base import RuntimeFailure
from tests.runtime_helpers import make_spec

SHIM = textwrap.dedent(
    """\
    #!{py}
    import os, socket, sys, time
    a = sys.argv[1:]
    if a[:2] != ["login", "debian"]:
        sys.exit(3)
    bind = a[a.index("--bind") + 1]
    host = bind.split(":")[0]
    if os.path.exists({fail!r}):
        open(os.path.join(host, "error.txt"), "w").write("Tor did not finish connecting in time.")
        sys.exit(1)
    s = socket.socket(socket.AF_UNIX)
    s.bind(os.path.join(host, "vnc.sock"))
    open(os.path.join(host, "profile-cookie"), "w").write("secret")
    open({dump!r}, "w").write(repr(sorted(os.environ)))
    time.sleep(60)
    """
)


def build_rootfs(root: Path) -> Path:
    for rel in ("usr/bin/chromium", "usr/bin/Xvnc", "usr/bin/tor", "usr/share/novnc/core/rfb.js"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return root


@unittest.skipIf(sys.platform.startswith("win"), "POSIX only")
class ProotBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.shim = self.tmp / "proot-distro"
        fail, dump = str(self.tmp / "fail"), str(self.tmp / "env.txt")
        self.shim.write_text(SHIM.format(py=sys.executable, fail=fail, dump=dump))
        self.shim.chmod(self.shim.stat().st_mode | stat.S_IXUSR)
        self.rootfs = build_rootfs(self.tmp / "rootfs")
        self.dump = self.tmp / "env.txt"
        os.environ["ORBITNEST_SECRET_KEY"] = "must-not-leak"
        self.rt = pb.ProotBrowserRuntime(
            self.tmp / "state", proot_bin=str(self.shim), rootfs=self.rootfs, startup_seconds=10
        )
        self.rids: list[str] = []

    def tearDown(self) -> None:
        for rid in self.rids:
            self.rt.remove(rid)
        for k in ("ORBITNEST_SECRET_KEY",):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def make(self) -> str:
        rid = self.rt.create(make_spec(None))
        self.rids.append(rid)
        return rid

    def test_not_ready_reports_fix_until_userland_complete(self) -> None:
        empty = pb.ProotBrowserRuntime(self.tmp / "s2", proot_bin=str(self.shim), rootfs=self.tmp / "none")
        why, fix = empty.not_ready() or ("", "")
        self.assertIn("setup-phone-browser", fix)
        self.assertIn("not set up", why)
        self.assertIsNone(self.rt.not_ready())

    def test_missing_proot_is_reported(self) -> None:
        rt = pb.ProotBrowserRuntime(self.tmp / "s3", proot_bin="definitely-not-installed", rootfs=self.rootfs)
        self.assertIn("proot-distro", (rt.not_ready() or ("",))[0])

    def test_start_exposes_screen_socket_and_stop_wipes_everything(self) -> None:
        rid = self.make()
        self.rt.start(rid)
        self.assertEqual(self.rt.inspect(rid).state, "running")
        session = self.rt._session(rid)
        self.assertTrue((session / "run.sh").is_file())
        self.assertTrue((session / "profile-cookie").is_file())
        self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o700)
        argv, _env = self.rt.stream_bridge_argv(rid, 5900)
        self.assertEqual(argv[:2], ["socat", "-"])
        self.assertTrue(argv[2].startswith("UNIX-CONNECT:"))
        self.rt.stop(rid, 2)
        self.assertFalse(session.exists(), "session folder (profile, cookies, Tor state) must be erased")
        self.assertEqual(self.rt.inspect(rid).state, "exited")

    def test_restart_never_reuses_previous_session(self) -> None:
        rid = self.make()
        self.rt.start(rid)
        session = self.rt._session(rid)
        (session / "leftover").write_text("old")
        self.rt.stop(rid, 2)
        self.rt.start(rid)
        self.assertFalse((session / "leftover").exists())
        self.rt.stop(rid, 2)

    def test_server_secret_is_not_passed_to_the_browser_process(self) -> None:
        rid = self.make()
        self.rt.start(rid)
        self.assertNotIn("ORBITNEST_SECRET_KEY", self.dump.read_text())
        self.rt.stop(rid, 2)

    def test_failed_start_reports_reason_and_wipes(self) -> None:
        (self.tmp / "fail").write_text("1")
        rid = self.make()
        with self.assertRaises(RuntimeFailure) as cm:
            self.rt.start(rid)
        self.assertIn("Tor did not finish", str(cm.exception))
        self.assertFalse(self.rt._session(rid).exists())

    def test_start_refused_when_not_ready(self) -> None:
        (self.rootfs / "usr/bin/tor").unlink()
        rid = self.make()
        with self.assertRaises(RuntimeFailure) as cm:
            self.rt.start(rid)
        self.assertIn("setup-phone-browser", str(cm.exception))

    def test_remove_deletes_state(self) -> None:
        rid = self.make()
        self.rt.start(rid)
        self.rt.remove(rid)
        self.assertEqual(self.rt.inspect(rid).state, "missing")
        self.assertEqual(self.rt.list_managed(), {})

    def test_asset_argv_confined_to_novnc_folder(self) -> None:
        argv, _ = self.rt.asset_argv("x", "core/rfb.js")
        self.assertEqual(argv[0], "cat")
        for bad in ("../../bin/chromium", "/etc/passwd", "core/missing.js"):
            with self.assertRaises(RuntimeFailure):
                self.rt.asset_argv("x", bad)

    def test_invalid_resource_id_rejected(self) -> None:
        with self.assertRaises(RuntimeFailure):
            self.rt.start("../escape")

    def test_run_script_fails_closed_and_forces_tor_proxy(self) -> None:
        script = pb.run_script(9150)
        self.assertLess(script.index("Bootstrapped 100%"), script.index("chromium "))
        self.assertIn('--proxy-server="socks5://127.0.0.1:9150"', script)
        self.assertIn("MAP * ~NOTFOUND", script)
        self.assertIn("--disable-quic", script)
        self.assertIn("-rfbport -1", script)  # no TCP listener for the screen
        self.assertIn("-nolisten tcp", script)

    def test_setup_commands_install_packages_and_policy(self) -> None:
        steps = pb.setup_commands("proot-distro", self.tmp / "absent")
        self.assertEqual(steps[0][:2], ["proot-distro", "install"])
        joined = " ".join(steps[-1])
        for pkg in pb.PACKAGES:
            self.assertIn(pkg, joined)
        self.assertIn("orbitnest-privacy.json", joined)

    def test_free_port_is_loopback_bindable(self) -> None:
        port = pb.free_port()
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
