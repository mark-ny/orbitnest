"""Platform detection tests.

Termux, Windows and WSL cases are SIMULATED by injecting environment/platform strings and a fake
command runner. They verify decision logic only, not behavior on real devices.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from app.core.platform import CommandResult, detect_capabilities, detect_platform, run_command

TERMUX_ENV = {
    "TERMUX_VERSION": "0.118.1",
    "PREFIX": "/data/data/com.termux/files/usr",
    "HOME": "/data/data/com.termux/files/home",
}


def fake_runner(daemon_ok: bool, compose_ok: bool = True, calls: list | None = None):
    def run(argv: Sequence[str], timeout: float) -> CommandResult:
        if calls is not None:
            calls.append(list(argv))
        if argv[1:2] == ["version"]:
            return CommandResult(0, "27.3.1", "") if daemon_ok else CommandResult(1, "", "cannot connect")
        if argv[1:3] == ["compose", "version"]:
            return CommandResult(0, "2.29.7", "") if compose_ok else CommandResult(1, "", "")
        return CommandResult(127, "", "")

    return run


def which_with(*available: str):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class DetectPlatformTests(unittest.TestCase):
    def test_termux_detected_by_env(self) -> None:
        info = detect_platform(TERMUX_ENV, sys_platform="linux", machine="aarch64", release="4.14")
        self.assertEqual(info.os_family, "android-termux")
        self.assertTrue(info.is_termux)
        self.assertEqual(info.architecture, "aarch64")

    def test_termux_detected_by_prefix_only(self) -> None:
        info = detect_platform({"PREFIX": "/data/data/com.termux/files/usr"}, sys_platform="linux")
        self.assertTrue(info.is_termux)

    def test_windows(self) -> None:
        self.assertEqual(detect_platform({}, sys_platform="win32").os_family, "windows")

    def test_wsl_by_kernel_release(self) -> None:
        info = detect_platform({}, sys_platform="linux", release="5.15.0-microsoft-standard-WSL2")
        self.assertTrue(info.is_wsl)
        self.assertEqual(info.os_family, "linux")

    def test_plain_linux_is_not_wsl_or_termux(self) -> None:
        info = detect_platform({}, sys_platform="linux", release="6.1.0-generic")
        self.assertFalse(info.is_wsl)
        self.assertFalse(info.is_termux)


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def caps(self, env, plat, daemon_ok=True, available=("docker",), calls=None, **kw):
        return detect_capabilities(
            self.dir,
            environ=env,
            sys_platform=plat,
            release=kw.get("release", "6.1.0"),
            which=which_with(*available),
            runner=fake_runner(daemon_ok, calls=calls),
        )

    def test_linux_with_docker(self) -> None:
        c = self.caps({}, "linux")
        self.assertEqual(c.mode, "linux-docker")
        self.assertEqual(c.features["container_workspaces"].state, "available")
        self.assertTrue(c.runtimes[0].available)
        self.assertEqual(c.runtimes[0].version, "27.3.1")

    def test_linux_cli_present_but_daemon_down(self) -> None:
        c = self.caps({}, "linux", daemon_ok=False)
        self.assertEqual(c.mode, "linux-no-container")
        self.assertEqual(c.features["container_workspaces"].state, "unsupported")
        self.assertIn("daemon is not reachable", c.runtimes[0].detail)
        self.assertIn("root-equivalent", c.runtimes[0].remediation or "")

    def test_linux_without_docker_cli(self) -> None:
        c = self.caps({}, "linux", available=())
        self.assertEqual(c.mode, "linux-no-container")
        self.assertEqual(c.runtimes[0].detail, "docker CLI not found")

    def test_windows_with_docker_desktop(self) -> None:
        self.assertEqual(self.caps({}, "win32").mode, "windows-docker-desktop")

    def test_windows_without_docker_gives_wsl_remediation(self) -> None:
        c = self.caps({}, "win32", available=())
        self.assertEqual(c.mode, "windows-no-container")
        self.assertIn("WSL 2", c.runtimes[0].remediation or "")

    def test_termux_never_probes_docker_even_if_cli_exists(self) -> None:
        calls: list = []
        c = self.caps(TERMUX_ENV, "linux", available=("docker", "proot-distro"), calls=calls)
        self.assertEqual(calls, [], "must not run docker in Termux")
        self.assertEqual(c.mode, "termux-local")
        self.assertEqual(c.features["container_workspaces"].state, "remote_required")
        self.assertEqual(c.features["local_process_workspaces"].state, "limited")
        self.assertIn("not a security boundary", c.runtimes[1].detail.lower())

    def test_termux_without_proot(self) -> None:
        c = self.caps(TERMUX_ENV, "linux", available=())
        self.assertEqual(c.features["proot_userland"].state, "unsupported")

    def test_unimplemented_features_are_not_advertised(self) -> None:
        c = self.caps({}, "linux")
        self.assertEqual(c.features["desktop_streaming"].state, "not_implemented")
        self.assertEqual(c.features["browser_terminal"].state, "available")
        self.assertEqual(c.features["file_manager"].state, "available")

    def test_storage_writable_and_unwritable(self) -> None:
        self.assertTrue(self.caps({}, "linux").storage.writable)
        blocker = self.dir / "file"
        blocker.write_text("x")
        c = detect_capabilities(
            blocker / "sub", environ={}, sys_platform="linux", which=which_with(), runner=fake_runner(False)
        )
        self.assertFalse(c.storage.writable)
        self.assertNotIn(str(blocker), c.storage.detail)


class RunCommandTests(unittest.TestCase):
    def test_missing_command_returns_127(self) -> None:
        self.assertEqual(run_command(["definitely-not-a-real-command-xyz"]).returncode, 127)

    def test_no_shell_interpretation(self) -> None:
        r = run_command(["echo", "a; echo injected"])
        self.assertEqual(r.stdout, "a; echo injected")

    def test_timeout(self) -> None:
        self.assertEqual(run_command(["sleep", "5"], 0.2).returncode, 124)


if __name__ == "__main__":
    unittest.main()
