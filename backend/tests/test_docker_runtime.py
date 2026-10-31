"""Docker adapter tests.

Verified here: argv hardening, error handling and state parsing against a SCRIPTED fake CLI, and the
existence of every flag we use in the REAL installed docker CLI's --help (client-side, no daemon).
NOT verified: behaviour against a real Docker daemon (none reachable in the build sandbox).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from app.core.platform import CommandResult
from app.runtimes.base import RuntimeFailure, RuntimeSpecError
from app.runtimes.docker_runtime import NETWORK_NAME, DockerRuntime
from app.services.templates import BUILTIN_TEMPLATES
from tests.runtime_helpers import WSID, make_spec

CID = "a" * 64
FORBIDDEN_FLAGS = {
    "--privileged",
    "--publish",
    "-p",
    "-P",
    "--publish-all",
    "--device",
    "--pid",
    "--userns",
    "--uts",
    "--volumes-from",
    "--volume",
    "-v",
    "--add-host",
    "--cgroupns",
    "--security-opt=seccomp=unconfined",
}


class ScriptedDocker:
    """Records argv and answers like a very small docker CLI."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.network_exists = False
        self.state = {"Status": "created", "ExitCode": 0}
        self.stop_works = True
        self.rc_override: dict[str, CommandResult] = {}

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append(argv)
        verb = " ".join(argv[1:3]) if argv[1] == "network" else argv[1]
        if verb in self.rc_override:
            return self.rc_override[verb]
        if verb == "network inspect":
            return CommandResult(
                0 if self.network_exists else 1, "", "" if self.network_exists else "Error: No such network"
            )
        if verb == "network create":
            self.network_exists = True
            return CommandResult(0, "netid", "")
        if verb == "create":
            return CommandResult(0, CID, "")
        if verb == "start":
            self.state = {"Status": "running", "ExitCode": 0}
            return CommandResult(0, argv[-1], "")
        if verb == "stop":
            if self.stop_works:
                self.state = {"Status": "exited", "ExitCode": 143}
            return CommandResult(0, argv[-1], "")
        if verb == "kill":
            self.state = {"Status": "exited", "ExitCode": 137}
            return CommandResult(0, argv[-1], "")
        if verb == "rm":
            return CommandResult(0, argv[-1], "")
        if verb == "inspect":
            return CommandResult(0, json.dumps(self.state), "")
        if verb == "ps":
            return CommandResult(
                0, json.dumps({"ID": CID, "Labels": f"orbitnest.managed=true,orbitnest.workspace={WSID}"}), ""
            )
        return CommandResult(1, "", "unexpected")

    def verbs(self):
        return [c[1] for c in self.calls]


class GatewayDocker(ScriptedDocker):
    """Adds what the Tor gateway flow asks docker for."""

    def __init__(self, healthy: bool = True) -> None:
        super().__init__()
        self.healthy = healthy

    def __call__(self, argv, timeout):
        argv = list(argv)
        if argv[1] == "inspect" and "{{.Name}}" in " ".join(argv):
            self.calls.append(argv)
            return CommandResult(0, f"/orbitnest-ws-x container:orbitnest-tor-{WSID.lower()}\n", "")
        if argv[1] == "inspect" and argv[-1].startswith("orbitnest-tor-"):
            self.calls.append(argv)
            state = {"Status": "running", "Health": {"Status": "healthy" if self.healthy else "starting"}}
            return CommandResult(0, json.dumps(state), "")
        return super().__call__(argv, timeout)


class TorGatewayTests(unittest.TestCase):
    def rt(self, **kw):
        cli = GatewayDocker(**kw)
        return cli, DockerRuntime(run=cli, user="1000:1000", sleep=lambda _s: None)

    def test_gateway_argv_is_locked_down(self) -> None:
        _cli, rt = self.rt()
        a = rt.build_gateway_argv(WSID)
        joined = " ".join(a)
        self.assertFalse(FORBIDDEN_FLAGS & set(a))
        self.assertEqual(a[a.index("--cap-drop") + 1], "ALL")
        added = {a[i + 1] for i, x in enumerate(a) if x == "--cap-add"}
        self.assertIn("NET_ADMIN", added)  # only the gateway may touch the firewall
        self.assertFalse(added & {"SYS_ADMIN", "NET_RAW", "SYS_MODULE", "SYS_PTRACE"})
        for needle in ("--read-only", "no-new-privileges:true", "--pids-limit 128", "--memory 256m"):
            self.assertIn(needle, joined)
        self.assertEqual(a[-1], "orbitnest/tor-gateway:1")
        self.assertIn("orbitnest.role=tor-gateway", joined)
        self.assertNotIn("--mount", a)

    def test_workspace_joins_the_gateway_namespace_with_no_caps(self) -> None:
        _cli, rt = self.rt()
        a = rt.build_create_argv(make_spec("/srv/x", egress="tor"))
        self.assertEqual(a[a.index("--network") + 1], f"container:orbitnest-tor-{WSID.lower()}")
        self.assertNotIn("--cap-add", a)
        self.assertNotIn("--dns", a)

    def test_workload_can_never_hold_net_admin_with_tor(self) -> None:
        with self.assertRaises(RuntimeSpecError):
            make_spec("/srv/x", egress="tor", capabilities=("NET_ADMIN",)).validate()
        with self.assertRaises(RuntimeSpecError):
            make_spec("/srv/x", egress="tor", network="none").validate()
        with self.assertRaises(RuntimeSpecError):
            make_spec("/srv/x", egress="vpn").validate()

    def test_create_starts_gateway_and_waits_before_creating_workspace(self) -> None:
        cli, rt = self.rt()
        rt.create(make_spec("/srv/x", egress="tor"))
        creates = [c for c in cli.calls if c[1] == "create"]
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0][-1], "orbitnest/tor-gateway:1")  # gateway first
        self.assertEqual(creates[1][creates[1].index("--network") + 1][:10], "container:")
        order = [c[1] for c in cli.calls]
        self.assertLess(order.index("start"), order.index("create", order.index("create") + 1))

    def test_unhealthy_gateway_means_the_workspace_is_never_created(self) -> None:
        import app.runtimes.docker_runtime as dr

        old = dr.GATEWAY_READY_SECONDS
        dr.GATEWAY_READY_SECONDS = 0.05
        try:
            cli, rt = self.rt(healthy=False)
            with self.assertRaises(RuntimeFailure) as cm:
                rt.create(make_spec("/srv/x", egress="tor"))
        finally:
            dr.GATEWAY_READY_SECONDS = old
        self.assertIn("nothing could leak", cm.exception.safe_message)
        self.assertEqual(len([c for c in cli.calls if c[1] == "create"]), 1)  # only the gateway
        self.assertTrue(any(c[1] == "rm" and c[-1].startswith("orbitnest-tor-") for c in cli.calls))

    def test_start_brings_the_gateway_up_first_and_remove_cleans_it(self) -> None:
        cli, rt = self.rt()
        rt.start(CID)
        starts = [c[-1] for c in cli.calls if c[1] == "start"]
        self.assertEqual(starts, [f"orbitnest-tor-{WSID.lower()}", CID])
        cli.calls.clear()
        rt.remove(CID)
        removed = [c[-1] for c in cli.calls if c[1] == "rm"]
        self.assertEqual(removed, [CID, f"orbitnest-tor-{WSID.lower()}"])

    def test_gateways_are_not_listed_as_orphans(self) -> None:
        cli = ScriptedDocker()
        rt = DockerRuntime(run=cli, user="1000:1000")
        listing = json.dumps(
            {
                "ID": "b" * 64,
                "Labels": f"orbitnest.managed=true,orbitnest.workspace={WSID},orbitnest.role=tor-gateway",
            }
        )
        cli.rc_override["ps"] = CommandResult(0, listing, "")
        self.assertEqual(rt.list_managed(), {})

    def test_gateways_are_listed_separately_for_orphan_cleanup(self) -> None:
        cli = ScriptedDocker()
        rt = DockerRuntime(run=cli, user="1000:1000")
        listing = "\n".join(
            json.dumps({"ID": c * 64, "Labels": f"orbitnest.managed=true,orbitnest.workspace={WSID},{extra}"})
            for c, extra in (("b", "orbitnest.role=tor-gateway"), ("c", "orbitnest.role=workspace"))
        )
        cli.rc_override["ps"] = CommandResult(0, listing, "")
        self.assertEqual(list(rt.list_gateways()), ["b" * 64])
        self.assertEqual(list(rt.list_managed()), ["c" * 64])

    def test_tor_check_runs_inside_the_gateway_and_parses_the_answer(self) -> None:
        class Cli(GatewayDocker):
            def __call__(self, argv, timeout):
                if "orbitnest-check" in " ".join(argv):
                    self.calls.append(list(argv))
                    return CommandResult(0, '{"IsTor":true,"IP":"198.51.100.4"}', "")
                return super().__call__(argv, timeout)

        cli = Cli()
        rt = DockerRuntime(run=cli, user="1000:1000", sleep=lambda _s: None)
        self.assertEqual(rt.tor_check(CID), {"is_tor": True, "exit_ip": "198.51.100.4"})
        self.assertTrue(any(c[1] == "exec" and c[2].startswith("orbitnest-tor-") for c in cli.calls))

    def test_gateway_image_names_agree(self) -> None:
        from app.runtimes.docker_runtime import TOR_GATEWAY_IMAGE
        from app.services import images

        self.assertEqual(images.HELPER_IMAGES["tor-gateway"], TOR_GATEWAY_IMAGE)


class StreamArgvTests(unittest.TestCase):
    def rt(self):
        return DockerRuntime(run=ScriptedDocker(), user="1000:1000")

    def test_shm_size_is_set_and_no_port_is_ever_published(self) -> None:
        argv = self.rt().build_create_argv(make_spec("/srv/x", shm_mb=512, stream_port=5900))
        self.assertEqual(argv[argv.index("--shm-size") + 1], "512m")
        self.assertFalse(FORBIDDEN_FLAGS & set(argv))
        self.assertFalse(any(a.startswith("--publish") or a == "-p" for a in argv))

    def test_bridge_is_a_docker_exec_pipe_to_loopback_inside_the_container(self) -> None:
        argv, _env = self.rt().stream_bridge_argv(CID, 5900)
        self.assertEqual(argv, ["docker", "exec", "-i", CID, "socat", "-", "TCP:127.0.0.1:5900"])
        self.assertNotIn("-t", argv)  # no tty: the stream must stay binary-clean
        for bad in (0, 70000):
            with self.assertRaises(RuntimeFailure):
                self.rt().stream_bridge_argv(CID, bad)
        with self.assertRaises(RuntimeFailure):
            self.rt().stream_bridge_argv("not-an-id; rm -rf /", 5900)

    def test_asset_paths_are_confined_to_the_novnc_directory(self) -> None:
        argv, _env = self.rt().asset_argv(CID, "core/rfb.js")
        self.assertEqual(argv, ["docker", "exec", CID, "cat", "/usr/share/novnc/core/rfb.js"])
        for bad in (
            "../etc/passwd",
            "core/../../x.js",
            "/etc/passwd",
            "core/x.sh",
            "app/ui.js",
            "-core/x.js",
            "core/x.js\n",
        ):
            with self.subTest(path=bad), self.assertRaises(RuntimeFailure):
                self.rt().asset_argv(CID, bad)

    def test_has_image(self) -> None:
        cli = ScriptedDocker()
        rt = DockerRuntime(run=cli, user="1000:1000")
        cli.rc_override["image"] = CommandResult(0, "sha256:abc", "")
        self.assertIs(rt.has_image("orbitnest/stream-tor:1"), True)
        cli.rc_override["image"] = CommandResult(1, "", "Error: No such image: x")
        self.assertIs(rt.has_image("orbitnest/stream-tor:1"), False)
        cli.rc_override["image"] = CommandResult(1, "", "Cannot connect to the Docker daemon")
        self.assertIsNone(rt.has_image("orbitnest/stream-tor:1"))


class ArgvTests(unittest.TestCase):
    def build(self, **kw):
        rt = DockerRuntime(run=ScriptedDocker(), user="1000:1000")
        return rt.build_create_argv(make_spec("/srv/orbitnest/ws/data", **kw))

    def test_hardening_flags_present(self) -> None:
        a = self.build()
        joined = " ".join(a)
        for needle in (
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            "--init",
            "--user 1000:1000",
            "--pids-limit 256",
            "--memory 512m",
            "--memory-swap 512m",
            "--cpus 0.50",
            "--restart no",
            "--ipc private",
            "--ulimit core=0",
            f"--network {NETWORK_NAME}",
        ):
            self.assertIn(needle, joined)

    def test_never_user_zero_or_forbidden_flags(self) -> None:
        a = self.build()
        self.assertNotEqual(a[a.index("--user") + 1].split(":")[0], "0")
        self.assertFalse(FORBIDDEN_FLAGS & set(a), FORBIDDEN_FLAGS & set(a))
        self.assertNotIn("docker.sock", " ".join(a))
        self.assertNotIn("host", [a[i + 1] for i, x in enumerate(a[:-1]) if x in ("--network", "--ipc")])

    def test_only_mount_is_the_data_dir(self) -> None:
        a = self.build()
        mounts = [a[i + 1] for i, x in enumerate(a) if x == "--mount"]
        self.assertEqual(mounts, ["type=bind,source=/srv/orbitnest/ws/data,target=/home/workspace"])

    def test_network_none(self) -> None:
        a = self.build(network="none")
        self.assertEqual(a[a.index("--network") + 1], "none")

    def test_no_cap_add_by_default_and_allowlisted_add(self) -> None:
        self.assertNotIn("--cap-add", self.build())
        a = self.build(capabilities=("NET_BIND_SERVICE",))
        self.assertEqual(a[a.index("--cap-add") + 1], "NET_BIND_SERVICE")

    def test_lab_root_still_unprivileged_with_explicit_caps_only(self) -> None:
        a = self.build(run_as_root=True, capabilities=("NET_RAW",))
        self.assertEqual(a[a.index("--user") + 1], "0:0")
        added = [a[i + 1] for i, x in enumerate(a) if x == "--cap-add"]
        self.assertIn("NET_RAW", added)
        self.assertIn("SETUID", added)
        for never in ("SYS_ADMIN", "SYS_MODULE", "SYS_RAWIO", "MKNOD", "SETFCAP", "NET_ADMIN", "ALL"):
            self.assertNotIn(never, added)
        self.assertEqual(a[a.index("--cap-drop") + 1], "ALL")
        self.assertIn("no-new-privileges:true", a)
        self.assertIn("HOME=/root", a)
        self.assertFalse(FORBIDDEN_FLAGS & set(a))
        self.assertNotIn("--privileged", a)

    def test_root_is_never_the_default(self) -> None:
        a = self.build()
        self.assertNotEqual(a[a.index("--user") + 1].split(":")[0], "0")

    def test_image_then_command_are_last_and_options_come_first(self) -> None:
        a = self.build()
        self.assertEqual(a[-3:], ["python:3.12-slim", "sleep", "infinity"])
        first_positional = a.index("python:3.12-slim")
        self.assertTrue(all(not x.startswith("--") for x in a[first_positional:]))

    def test_healthcheck_optional(self) -> None:
        self.assertNotIn("--health-cmd", self.build())
        self.assertIn("--health-cmd", self.build(healthcheck=("true",)))

    def test_every_builtin_template_produces_safe_argv(self) -> None:
        for t in BUILTIN_TEMPLATES:
            with self.subTest(t=t.id):
                a = self.build(image=t.image, command=t.command, capabilities=t.capabilities)
                self.assertFalse(FORBIDDEN_FLAGS & set(a))
                self.assertIn("--cap-drop", a)

    def test_unsafe_specs_never_reach_docker(self) -> None:
        runner = ScriptedDocker()
        rt = DockerRuntime(run=runner)
        bad_paths = ["/srv/a,b", '/srv/a"b', "relative/path", "/srv/x\n--privileged"]
        for p in bad_paths:
            with self.subTest(path=p), self.assertRaises(RuntimeSpecError):
                rt.create(make_spec(p))
        for kw in (dict(image="--privileged"), dict(network="host"), dict(capabilities=("SYS_ADMIN",))):
            with self.assertRaises(RuntimeSpecError):
                rt.create(make_spec("/srv/ok", **kw))
        self.assertEqual(runner.calls, [], "docker must not be invoked for invalid specs")

    def test_windows_drive_path_accepted(self) -> None:
        rt = DockerRuntime(run=ScriptedDocker(), user="1000:1000")
        a = rt.build_create_argv(make_spec("C:\\Users\\me\\AppData\\Local\\OrbitNest\\workspaces\\x\\data"))
        self.assertIn("source=C:\\Users\\me", " ".join(a))

    def test_name_and_labels(self) -> None:
        a = self.build()
        self.assertEqual(a[a.index("--name") + 1], f"orbitnest-ws-{WSID.lower()}")
        self.assertIn(f"orbitnest.workspace={WSID}", a)


class BehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = ScriptedDocker()
        self.rt = DockerRuntime(run=self.cli, user="1000:1000", redact=("/srv/secret/data",))

    def test_create_ensures_network_once_and_returns_id(self) -> None:
        self.assertEqual(self.rt.create(make_spec("/srv/x")), CID)
        self.rt.create(make_spec("/srv/x"))
        self.assertEqual(self.cli.verbs().count("network"), 3)  # inspect+create, then inspect only
        net_create = next(c for c in self.cli.calls if c[1:3] == ["network", "create"])
        self.assertIn("com.docker.network.bridge.enable_icc=false", net_create)

    def test_network_none_skips_network_setup(self) -> None:
        self.rt.create(make_spec("/srv/x", network="none"))
        self.assertNotIn("network", self.cli.verbs())

    def test_lifecycle_calls(self) -> None:
        self.rt.start(CID)
        self.assertEqual(self.rt.inspect(CID).state, "running")
        self.rt.stop(CID, 7)
        stop = next(c for c in self.cli.calls if c[1] == "stop")
        self.assertEqual(stop[2:4], ["-t", "7"])
        self.assertEqual(self.rt.inspect(CID).state, "exited")
        self.assertNotIn("kill", self.cli.verbs())
        self.rt.remove(CID)
        self.assertEqual(next(c for c in self.cli.calls if c[1] == "rm")[2], "-f")

    def test_stop_escalates_to_kill_when_still_running(self) -> None:
        self.cli.state = {"Status": "running"}
        self.cli.stop_works = False
        self.rt.stop(CID, 1)
        self.assertIn("kill", self.cli.verbs())
        self.assertEqual(self.rt.inspect(CID).state, "exited")

    def test_stop_fails_loudly_if_nothing_works(self) -> None:
        self.cli.state = {"Status": "running"}
        self.cli.stop_works = False
        self.cli.rc_override["kill"] = CommandResult(0, "", "")
        with self.assertRaises(RuntimeFailure):
            self.rt.stop(CID, 1)

    def test_missing_container_is_tolerated_on_stop_and_remove(self) -> None:
        gone = CommandResult(1, "", "Error response from daemon: No such container: x")
        self.cli.rc_override.update(stop=gone, rm=gone, inspect=gone)
        self.rt.stop(CID, 1)
        self.rt.remove(CID)
        self.assertEqual(self.rt.inspect(CID).state, "missing")

    def test_errors_are_sanitised(self) -> None:
        self.cli.rc_override["create"] = CommandResult(
            1,
            "",
            "docker: Error: invalid mount config: bind path /srv/secret/data missing\nsecond line",
        )
        with self.assertRaises(RuntimeFailure) as ctx:
            self.rt.create(make_spec("/srv/secret/data"))
        self.assertNotIn("/srv/secret/data", ctx.exception.safe_message)
        self.assertIn("<data>", ctx.exception.safe_message)

    def test_cli_missing_and_timeout_messages(self) -> None:
        self.cli.rc_override["start"] = CommandResult(127, "", "docker CLI not found")
        with self.assertRaises(RuntimeFailure) as ctx:
            self.rt.start(CID)
        self.assertIn("not available", ctx.exception.safe_message)
        self.cli.rc_override["start"] = CommandResult(124, "", "timed out")
        with self.assertRaises(RuntimeFailure) as ctx2:
            self.rt.start(CID)
        self.assertIn("timed out", ctx2.exception.safe_message)

    def test_resource_ids_validated_before_use(self) -> None:
        for bad in ("--privileged", "-f", "abc", "x" * 64, "../etc", CID + ";rm"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeFailure):
                self.rt.start(bad)
        self.assertEqual(self.cli.calls, [])

    def test_unexpected_create_output_rejected(self) -> None:
        self.cli.rc_override["create"] = CommandResult(0, "WARNING: something\nnot-an-id", "")
        with self.assertRaises(RuntimeFailure):
            self.rt.create(make_spec("/srv/x"))

    def test_inspect_health_and_exit_parsing(self) -> None:
        self.cli.state = {"Status": "running", "Health": {"Status": "unhealthy"}}
        s = self.rt.inspect(CID)
        self.assertEqual((s.state, s.healthy), ("running", False))
        self.cli.state = {"Status": "running", "Health": {"Status": "starting"}}
        self.assertIsNone(self.rt.inspect(CID).healthy)
        self.cli.state = {"Status": "exited", "ExitCode": 137}
        s = self.rt.inspect(CID)
        self.assertEqual((s.state, s.exit_code), ("exited", 137))
        self.cli.rc_override["inspect"] = CommandResult(0, "not json", "")
        self.assertEqual(self.rt.inspect(CID).state, "unknown")

    def test_list_managed(self) -> None:
        self.assertEqual(
            self.rt.list_managed(), {CID: {"orbitnest.managed": "true", "orbitnest.workspace": WSID}}
        )


@unittest.skipUnless(shutil.which("docker"), "docker CLI not installed")
class RealCliFlagTests(unittest.TestCase):
    """Every option we pass must exist in the installed CLI (client-side check, no daemon needed)."""

    @staticmethod
    def help_text(*sub: str) -> str:
        r = subprocess.run(["docker", *sub, "--help"], capture_output=True, text=True, timeout=30)
        return r.stdout + r.stderr

    def flags_before_positional(self, argv: list[str]) -> list[str]:
        return [x for x in argv if x.startswith("-") and not x.startswith("--health-cmd=")]

    def check(self, subcommand: tuple[str, ...], argv: list[str]) -> None:
        text = self.help_text(*subcommand)
        self.assertIn("Usage", text, f"could not read help for {subcommand}")
        for flag in self.flags_before_positional(argv):
            self.assertIn(flag, text, f"flag {flag} not found in `docker {' '.join(subcommand)} --help`")

    def test_create_flags_exist(self) -> None:
        rt = DockerRuntime(run=ScriptedDocker(), user="1000:1000")
        argv = rt.build_create_argv(
            make_spec("/srv/x", healthcheck=("true",), capabilities=("NET_BIND_SERVICE",))
        )
        self.check(("create",), argv)

    def test_other_subcommand_flags_exist(self) -> None:
        cli = ScriptedDocker()
        rt = DockerRuntime(run=cli, user="1000:1000")
        rt.create(make_spec("/srv/x"))
        rt.stop(CID, 5)
        rt.remove(CID)
        rt.inspect(CID)
        rt.list_managed()
        cli.state = {"Status": "running"}
        cli.stop_works = False
        rt.stop(CID, 1)  # triggers kill
        seen = {}
        for call in cli.calls:
            key = tuple(call[1:3]) if call[1] == "network" else (call[1],)
            seen[key] = call
        for key, call in seen.items():
            if key == ("create",):
                continue
            with self.subTest(sub=key):
                self.check(key, [x for x in call[1 + len(key) :] if x.startswith("-")])


if __name__ == "__main__":
    unittest.main()
