from __future__ import annotations

import itertools
import pathlib
import sqlite3
import tempfile
import unittest

from app.core import matrix
from app.core.platform import detect_capabilities
from app.db import Database, migrate
from app.services import lifecycle, resources
from app.services.templates import (
    BUILTIN_TEMPLATES,
    TemplateError,
    TemplateRegistry,
    evaluate,
    validate_custom_definition,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
TERMUX_ENV = {"TERMUX_VERSION": "0.118", "PREFIX": "/data/data/com.termux/files/usr", "HOME": "/h"}


def caps(env, plat="linux", docker=True, tmp="/tmp"):
    from app.core.platform import CommandResult

    def runner(argv, timeout):
        if argv[1:2] == ["version"]:
            return CommandResult(0, "27.0", "") if docker else CommandResult(1, "", "no daemon")
        return CommandResult(0, "2.0", "")

    return detect_capabilities(
        pathlib.Path(tmp),
        environ=env,
        sys_platform=plat,
        which=lambda n: f"/usr/bin/{n}" if (docker or n != "docker") else None,
        runner=runner,
    )


class LifecycleTests(unittest.TestCase):
    def test_every_state_defined_and_deleted_is_terminal(self) -> None:
        self.assertEqual(set(lifecycle.STATES), set(lifecycle.ALLOWED))
        self.assertEqual(lifecycle.ALLOWED["deleted"], frozenset())

    def test_targets_are_known_states(self) -> None:
        for src, targets in lifecycle.ALLOWED.items():
            self.assertLessEqual(targets, set(lifecycle.STATES), src)

    def test_happy_path(self) -> None:
        path = [
            "provisioning",
            "starting",
            "running",
            "idle",
            "running",
            "stopping",
            "stopped",
            "starting",
            "running",
            "deleting",
            "deleted",
        ]
        for a, b in itertools.pairwise(path):
            lifecycle.check_transition(a, b)

    def test_illegal_transitions(self) -> None:
        for a, b in [
            ("stopped", "running"),
            ("running", "starting"),
            ("deleted", "running"),
            ("stopped", "stopping"),
            ("provisioning", "running"),
            ("idle", "starting"),
        ]:
            with self.subTest(a=a, b=b), self.assertRaises(lifecycle.InvalidTransition):
                lifecycle.check_transition(a, b)

    def test_failed_is_recoverable_and_deletable(self) -> None:
        for target in ("starting", "provisioning", "deleting"):
            self.assertTrue(lifecycle.can_transition("failed", target))

    def test_reset_path_exists(self) -> None:
        self.assertTrue(lifecycle.can_transition("stopped", "provisioning"))

    def test_sets(self) -> None:
        self.assertEqual(lifecycle.TRANSITIONAL, {"provisioning", "starting", "stopping", "deleting"})
        self.assertIn("idle", lifecycle.ACTIVE)
        self.assertNotIn("stopped", lifecycle.ACTIVE)
        self.assertIn("stopping", lifecycle.sources_for("stopped"))


class ResourceTests(unittest.TestCase):
    def test_resolves_within_ceiling(self) -> None:
        self.assertEqual(resources.resolve_profile("small", max_profile="large").memory_mb, 512)
        self.assertEqual(resources.resolve_profile("medium", max_profile="medium").cpu_millis, 1000)

    def test_rejects_unknown_and_over_ceiling(self) -> None:
        for name, mx in [("huge", "large"), ("large", "medium"), ("medium", "small"), ("", "large")]:
            with self.subTest(name=name), self.assertRaises(resources.ProfileError):
                resources.resolve_profile(name, max_profile=mx)

    def test_all_profiles_within_hard_caps_and_increasing(self) -> None:
        prev = 0
        for name in resources.PROFILE_ORDER:
            r = resources.PROFILES[name]
            self.assertLessEqual(r.cpu_millis, resources.HARD_CAPS.cpu_millis)
            self.assertLessEqual(r.memory_mb, resources.HARD_CAPS.memory_mb)
            self.assertGreater(r.memory_mb, prev)
            prev = r.memory_mb


class TemplateTests(unittest.TestCase):
    def test_nine_builtin_profiles_plus_custom_mechanism(self) -> None:
        self.assertEqual(
            [t.id for t in BUILTIN_TEMPLATES],
            [
                "browser-chromium",
                "browser-firefox",
                "browser-tor",
                "desktop-ubuntu",
                "desktop-kali",
                "terminal-ubuntu",
                "dev-python",
                "dev-node",
                "dev-web",
                "security-lab",
            ],
        )  # the tenth profile, "Custom Template", is the admin-defined mechanism below

    def test_builtins_are_hardened_by_default(self) -> None:
        for t in BUILTIN_TEMPLATES:
            with self.subTest(t=t.id):
                self.assertEqual(t.published_ports, ())
                self.assertEqual(t.capabilities, ())
                self.assertIn("isolated-bridge", t.allowed_networks)
                self.assertFalse(t.image.startswith("-"))
                self.assertIn(t.matrix_key, matrix.MATRIX)

    def test_custom_templates_cannot_take_lab_privileges(self) -> None:
        from app.services.templates import TemplateError, validate_custom_definition

        base = {"id": "custom-x", "name": "x", "description": "d", "image": "alpine:3"}
        for extra in (
            {"capabilities": ["NET_RAW"]},
            {"capabilities": ["SYS_ADMIN"]},
            {"optional_capabilities": ["NET_RAW"], "authorized_use_only": True},
            {"run_as_root": True, "authorized_use_only": True},
            {"privacy": "tor"},
        ):
            with self.subTest(extra=extra), self.assertRaises(TemplateError):
                validate_custom_definition({**base, **extra})
        self.assertEqual(
            validate_custom_definition({**base, "capabilities": ["NET_BIND_SERVICE"]}).id, "custom-x"
        )

    def test_security_templates_flagged_authorized_use(self) -> None:
        by_id = {t.id: t for t in BUILTIN_TEMPLATES}
        self.assertTrue(by_id["security-lab"].authorized_use_only)
        self.assertTrue(by_id["desktop-kali"].authorized_use_only)
        self.assertTrue(by_id["desktop-kali"].enabled)
        self.assertTrue(by_id["security-lab"].optional_capabilities)

    def base(self, **kw):
        d = {
            "id": "custom-demo",
            "name": "Demo",
            "description": "d",
            "image": "registry.example/team/img:1.2",
            "command": ["sleep", "infinity"],
        }
        d.update(kw)
        return d

    def test_valid_custom(self) -> None:
        t = validate_custom_definition(self.base())
        self.assertEqual((t.category, t.builtin, t.matrix_key), ("custom", False, "custom"))

    def test_custom_rejections(self) -> None:
        cases = {
            "bad id prefix": self.base(id="demo"),
            "builtin collision": self.base(id="dev-python"),
            "image flag injection": self.base(image="--privileged"),
            "image with space": self.base(image="a b"),
            "image with shell": self.base(image="a;rm -rf /"),
            "image traversal": self.base(image="a/../b"),
            "bad digest": self.base(image_digest="sha256:123"),
            "command flag": self.base(command=["--privileged"]),
            "command nul": self.base(command=["a\x00b"]),
            "too many args": self.base(command=["x"] * 17),
            "ports": self.base(published_ports=[8080]),
            "dangerous cap": self.base(capabilities=["SYS_ADMIN"]),
            "all cap": self.base(capabilities=["ALL"]),
            "local process": self.base(local_supported=True),
            "local command": self.base(local_command=["bash"]),
            "mount root": self.base(data_mount="/"),
            "mount traversal": self.base(data_mount="/a/../etc"),
            "mount option injection": self.base(data_mount="/data,readonly=false"),
            "mount relative": self.base(data_mount="data"),
            "unknown profile": self.base(max_profile="gigantic"),
            "default above max": self.base(default_profile="large", max_profile="small"),
            "unknown field (privileged)": self.base(privileged=True),
            "unknown field (host network)": self.base(network_mode="host"),
            "host network policy": self.base(network_policy="host"),
        }
        for label, payload in cases.items():
            with self.subTest(label), self.assertRaises(TemplateError):
                validate_custom_definition(payload)

    def test_error_messages_do_not_echo_input(self) -> None:
        with self.assertRaises(TemplateError) as ctx:
            validate_custom_definition(self.base(image="evil image;secret-value-123"))
        self.assertNotIn("secret-value-123", str(ctx.exception))

    def test_disabled_requires_reason(self) -> None:
        with self.assertRaises(TemplateError):
            validate_custom_definition(self.base(enabled=False))


class MatrixTests(unittest.TestCase):
    def test_docs_table_matches_code(self) -> None:
        doc = (ROOT / "docs" / "PLATFORM_SUPPORT.md").read_text()
        block = doc.split("<!-- MATRIX:BEGIN -->\n")[1].split("\n<!-- MATRIX:END -->")[0]
        self.assertEqual(block, matrix.render_markdown())

    def test_every_builtin_has_a_row_and_states_are_valid(self) -> None:
        for t in BUILTIN_TEMPLATES:
            self.assertIn(t.id, matrix.MATRIX)
        for row in matrix.MATRIX.values():
            self.assertEqual(len(row), len(matrix.MODES))
            self.assertLessEqual(set(row), set(matrix.STATE_LABELS))

    def test_termux_never_claims_full_isolation(self) -> None:
        col = matrix.MODES.index("termux-local")
        for key, row in matrix.MATRIX.items():
            self.assertNotEqual(row[col], "full", key)


class EvaluateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.by_id = {t.id: t for t in BUILTIN_TEMPLATES}

    def test_linux_docker(self) -> None:
        c = caps({})
        a = evaluate(self.by_id["dev-python"], c)
        self.assertEqual(
            (a.creatable, a.runtime, a.isolation, a.state), (True, "docker", "container", "full")
        )
        self.assertTrue(evaluate(self.by_id["terminal-ubuntu"], c).creatable)
        self.assertTrue(evaluate(self.by_id["security-lab"], c).creatable)

    def test_streaming_profiles_need_their_locally_built_image(self) -> None:
        c = caps({})
        for tid in ("browser-chromium", "browser-firefox", "browser-tor", "desktop-ubuntu"):
            tpl = self.by_id[tid]
            self.assertTrue(evaluate(tpl, c, image_present=lambda _t: True).creatable, tid)
            self.assertTrue(evaluate(tpl, c).creatable, tid)  # unknown presence is not a blocker
            missing = evaluate(tpl, c, image_present=lambda _t: False)
            self.assertFalse(missing.creatable, tid)
            self.assertIn("not been built", missing.reason)
            self.assertIn("build-images", missing.remediation)
        # Images that come from a registry are never gated on a local build.
        self.assertTrue(evaluate(self.by_id["dev-python"], c, image_present=lambda _t: False).creatable)

    def test_disabled_builtin(self) -> None:
        off = self.by_id["desktop-kali"].model_copy(
            update={"enabled": False, "disabled_reason": "Kali is off"}
        )
        a = evaluate(off, caps({}))
        self.assertFalse(a.creatable)
        self.assertIn("Kali", a.reason)

    def test_windows_docker_desktop_security_lab_is_limited_but_creatable(self) -> None:
        a = evaluate(self.by_id["security-lab"], caps({}, plat="win32"))
        self.assertEqual((a.creatable, a.state), (True, "limited"))

    def test_no_docker_means_nothing_creatable_with_remediation(self) -> None:
        c = caps({}, docker=False)
        a = evaluate(self.by_id["dev-python"], c)
        self.assertFalse(a.creatable)
        self.assertTrue(a.remediation)

    def test_termux_local_has_no_isolation_and_limited_scope(self) -> None:
        c = caps(TERMUX_ENV)
        which = lambda n: f"/x/{n}"  # noqa: E731
        a = evaluate(self.by_id["dev-python"], c, which=which)
        self.assertEqual((a.creatable, a.runtime, a.isolation, a.state), (True, "local", "none", "limited"))
        self.assertFalse(evaluate(self.by_id["browser-chromium"], c, which=which).creatable)
        self.assertFalse(evaluate(self.by_id["security-lab"], c, which=which).creatable)
        self.assertIn("remote", evaluate(self.by_id["security-lab"], c, which=which).reason.lower())
        proot = evaluate(self.by_id["terminal-ubuntu"], c, which=which)
        self.assertFalse(proot.creatable)
        self.assertIn("proot", proot.reason)

    def test_termux_missing_toolchain(self) -> None:
        a = evaluate(self.by_id["dev-node"], caps(TERMUX_ENV), which=lambda n: None)
        self.assertFalse(a.creatable)
        self.assertIn("pkg install nodejs", a.remediation)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        migrate(self.db)
        self.reg = TemplateRegistry(self.db)
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES ('u1','admin','x','admin')"
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def defn(self, **kw):
        d = {
            "id": "custom-demo",
            "name": "Demo",
            "description": "d",
            "image": "img:1",
            "command": ["sleep", "infinity"],
        }
        d.update(kw)
        return d

    def test_builtins_listed_and_get(self) -> None:
        self.assertEqual(len(self.reg.list()), len(BUILTIN_TEMPLATES))
        self.assertEqual(self.reg.get("dev-node").id, "dev-node")
        self.assertIsNone(self.reg.get("nope"))
        self.assertIsNone(self.reg.get("custom-missing"))

    def test_custom_roundtrip_enable_disable_delete(self) -> None:
        self.reg.create_custom("u1", self.defn())
        self.assertEqual(self.reg.get("custom-demo").image, "img:1")
        self.assertEqual(len(self.reg.list()), len(BUILTIN_TEMPLATES) + 1)
        t = self.reg.set_enabled("custom-demo", False)
        self.assertFalse(t.enabled)
        self.assertTrue(t.disabled_reason)
        self.assertFalse(evaluate(t, caps({})).creatable)
        self.assertIsNone(self.reg.set_enabled("custom-nope", True))
        self.assertTrue(self.reg.delete_custom("custom-demo"))
        self.assertIsNone(self.reg.get("custom-demo"))

    def test_duplicate_custom_rejected(self) -> None:
        self.reg.create_custom("u1", self.defn())
        with self.assertRaises(TemplateError):
            self.reg.create_custom("u1", self.defn())

    def test_delete_refused_while_in_use(self) -> None:
        self.reg.create_custom("u1", self.defn())
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO workspaces (id, owner_id, name, template_id, runtime, status, persistence, "
                "resource_profile, cpu_millis, memory_mb, pids_limit, storage_quota_mb, network_policy, "
                "created_at, updated_at) VALUES ('w1','u1','w','custom-demo','fake','stopped','persistent',"
                "'small',500,512,256,2048,'none','t','t')"
            )
        with self.assertRaises(TemplateError):
            self.reg.delete_custom("custom-demo")


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        migrate(self.db)
        with self.db.transaction() as c:
            c.execute("INSERT INTO users (id, username, password_hash, role) VALUES ('u1','a','x','admin')")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def insert(self, c, wid="w1", name="One", status="stopped", **kw):
        vals = dict(owner="u1", runtime="fake", network="none", persistence="persistent")
        vals.update(kw)
        c.execute(
            "INSERT INTO workspaces (id, owner_id, name, template_id, runtime, status, persistence, "
            "resource_profile, "
            "cpu_millis, memory_mb, pids_limit, storage_quota_mb, network_policy, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wid,
                vals["owner"],
                name,
                "dev-python",
                vals["runtime"],
                status,
                vals["persistence"],
                "small",
                500,
                512,
                256,
                2048,
                vals["network"],
                "t",
                "t",
            ),
        )

    def test_constraints(self) -> None:
        for kw in (
            {"status": "bogus"},
            {"runtime": "k8s"},
            {"network": "host"},
            {"persistence": "forever"},
            {"owner": "nobody"},
        ):
            with self.subTest(kw=kw), self.assertRaises(sqlite3.IntegrityError), self.db.transaction() as c:
                self.insert(c, **kw)

    def test_name_unique_per_owner_case_insensitive_but_reusable_after_delete(self) -> None:
        with self.db.transaction() as c:
            self.insert(c, "w1", "Dev")
        with self.assertRaises(sqlite3.IntegrityError), self.db.transaction() as c:
            self.insert(c, "w2", "dEV")
        with self.db.transaction() as c:
            c.execute("UPDATE workspaces SET status='deleted' WHERE id='w1'")
            self.insert(c, "w3", "Dev")

    def test_cascade_removes_children(self) -> None:
        with self.db.transaction() as c:
            self.insert(c)
            c.execute("INSERT INTO workspace_events (id, workspace_id, at, event) VALUES ('e1','w1','t','x')")
            c.execute(
                "INSERT INTO runtime_instances (id, workspace_id, runtime, resource_id, created_at) "
                "VALUES ('i1','w1','fake','r1','t')"
            )
        with self.db.transaction() as c:
            c.execute("DELETE FROM workspaces WHERE id='w1'")
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workspace_events").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM runtime_instances").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
