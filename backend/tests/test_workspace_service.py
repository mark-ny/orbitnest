"""Workspace lifecycle against the fake runtime (real SQLite, real filesystem for data dirs)."""

from __future__ import annotations

import unittest

from app.core import errors
from app.runtimes.base import RuntimeSpec
from app.services import policy as policy_mod
from app.services.audit import list_events
from app.services.jobs import ManualRunner
from app.services.templates import LAB_RULES_VERSION
from tests.helpers import ServiceHarness, make_caps
from tests.runtime_helpers import WSID


def code_of(fn):
    try:
        fn()
    except errors.AppError as exc:
        return exc.code, exc.status_code
    raise AssertionError("expected AppError")


class LifecycleTests(ServiceHarness):
    def test_create_runs_to_running_and_records_everything(self) -> None:
        ws = self.make_ws()
        self.assertEqual(self.status(ws), "running")
        inst = self.store.current_instance(ws.id)
        self.assertIn(inst.resource_id, self.fake.resources)
        spec = self.fake.resources[inst.resource_id].spec
        self.assertEqual(spec.labels["orbitnest.workspace"], ws.id)
        self.assertTrue(self.service.storage.exists(ws.id))
        events = [e["event"] for e in reversed(self.store.events(ws.id))]
        self.assertEqual(events, ["created", "provisioned", "started"])

    def test_stop_start_restart(self) -> None:
        ws = self.make_ws()
        self.service.stop(self.alice, ws.id)
        self.assertEqual(self.status(ws), "stopped")
        self.service.start(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")
        rid = self.store.current_instance(ws.id).resource_id
        self.service.restart(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")
        self.assertEqual(self.store.current_instance(ws.id).resource_id, rid)
        self.assertGreaterEqual(self.fake.ops("stop").count(rid), 2)

    def test_invalid_transitions_are_conflicts(self) -> None:
        ws = self.make_ws()
        self.assertEqual(code_of(lambda: self.service.start(self.alice, ws.id)), ("workspace_busy", 409))
        self.service.stop(self.alice, ws.id)
        self.assertEqual(code_of(lambda: self.service.restart(self.alice, ws.id)), ("workspace_busy", 409))
        self.service.delete(self.alice, ws.id)
        self.assertEqual(code_of(lambda: self.service.start(self.alice, ws.id)), ("workspace_deleted", 409))

    def test_persistent_delete_retains_data_and_frees_name(self) -> None:
        ws = self.make_ws()
        (self.service.storage.volume_dir(ws.id) / "notes.txt").write_text("keep me")
        self.service.delete(self.alice, ws.id)
        self.assertEqual(self.status(ws), "deleted")
        self.assertEqual((self.service.storage.volume_dir(ws.id) / "notes.txt").read_text(), "keep me")
        self.assertEqual(self.store.get(ws.id).volume_state, "retained")
        self.assertEqual(self.fake.resources, {})
        self.make_ws()  # same name is free again

    def test_delete_data_needs_exact_name(self) -> None:
        ws = self.make_ws()
        (self.service.storage.volume_dir(ws.id) / "f").write_text("x")
        for bad in (None, "other", "DEV ONE "):
            self.assertEqual(
                code_of(
                    lambda bad=bad: self.service.delete(self.alice, ws.id, delete_data=True, confirm_name=bad)
                )[0],
                "confirmation_required",
            )
        self.assertEqual(self.status(ws), "running")
        self.service.delete(self.alice, ws.id, delete_data=True, confirm_name="dev one")
        self.assertFalse(self.service.storage.exists(ws.id))
        self.assertEqual(self.store.get(ws.id).volume_state, "purged")

    def test_disposable_semantics(self) -> None:
        ws = self.make_ws(persistence="disposable")
        (self.service.storage.volume_dir(ws.id) / "tmp").write_text("x")
        first = self.store.current_instance(ws.id).resource_id
        self.service.stop(self.alice, ws.id)
        self.assertFalse((self.service.storage.volume_dir(ws.id) / "tmp").exists())
        self.assertNotIn(first, self.fake.resources)
        self.service.start(self.alice, ws.id)  # no instance left: re-provisioned
        self.assertEqual(self.status(ws), "running")
        self.assertNotEqual(self.store.current_instance(ws.id).resource_id, first)
        self.service.delete(self.alice, ws.id)
        self.assertFalse(self.service.storage.exists(ws.id))

    def test_create_failure_then_retry(self) -> None:
        self.fake.fail_next("create")
        ws = self.make_ws()
        got = self.store.get(ws.id)
        self.assertEqual(got.status, "failed")
        self.assertIn("create failed", got.status_reason)
        self.service.start(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")

    def test_start_failure_then_retry(self) -> None:
        ws = self.make_ws()
        self.service.stop(self.alice, ws.id)
        self.fake.fail_next("start")
        self.service.start(self.alice, ws.id)
        self.assertEqual(self.status(ws), "failed")
        self.service.start(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")

    def test_unhealthy_and_exit_and_timeout(self) -> None:
        self.fake.health_after_start = False
        ws = self.make_ws()
        self.assertEqual(self.store.get(ws.id).status, "failed")
        self.assertIn("health check", self.store.get(ws.id).status_reason)

    def test_unexpected_exception_does_not_leak(self) -> None:
        def boom(op, rid):
            if op == "create":
                raise RuntimeError("secret /srv/orbitnest/private")

        self.fake.hook = boom
        ws = self.make_ws()
        got = self.store.get(ws.id)
        self.assertEqual(got.status, "failed")
        self.assertNotIn("secret", got.status_reason)
        self.assertNotIn("/srv", got.status_reason)

    def test_stop_failure_marks_failed_and_retry_works(self) -> None:
        ws = self.make_ws()
        self.fake.fail_next("stop")
        self.service.stop(self.alice, ws.id)
        self.assertEqual(self.status(ws), "failed")
        self.service.stop(self.alice, ws.id)
        self.assertEqual(self.status(ws), "stopped")

    def test_delete_failure_then_retry(self) -> None:
        ws = self.make_ws()
        self.fake.fail_next("remove")
        self.service.delete(self.alice, ws.id)
        self.assertEqual(self.status(ws), "failed")
        self.service.delete(self.alice, ws.id)
        self.assertEqual(self.status(ws), "deleted")

    def test_reset_keeps_data_unless_wiped(self) -> None:
        ws = self.make_ws()
        f = self.service.storage.volume_dir(ws.id) / "keep"
        f.write_text("x")
        old = self.store.current_instance(ws.id).resource_id
        self.assertEqual(code_of(lambda: self.service.reset(self.alice, ws.id))[0], "workspace_busy")
        self.service.stop(self.alice, ws.id)
        self.service.reset(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")
        self.assertTrue(f.exists())
        self.assertNotIn(old, self.fake.resources)
        self.assertNotEqual(self.store.current_instance(ws.id).resource_id, old)
        self.service.stop(self.alice, ws.id)
        self.assertEqual(
            code_of(lambda: self.service.reset(self.alice, ws.id, wipe_data=True))[0], "confirmation_required"
        )
        self.service.reset(self.alice, ws.id, wipe_data=True, confirm_name="dev one")
        self.assertFalse(f.exists())
        self.assertEqual(self.status(ws), "running")

    def test_update_fields(self) -> None:
        ws = self.make_ws()
        self.service.update(self.alice, ws.id, name="renamed", labels={"team": "a"}, idle_timeout_minutes=0)
        got = self.store.get(ws.id)
        self.assertEqual((got.name, got.labels, got.idle_timeout_minutes), ("renamed", {"team": "a"}, None))
        self.make_ws(name="second")
        self.assertEqual(
            code_of(lambda: self.service.update(self.alice, ws.id, name="SECOND"))[0], "conflict"
        )
        self.service.delete(self.alice, ws.id)
        self.assertEqual(
            code_of(lambda: self.service.update(self.alice, ws.id, name="x"))[0], "workspace_deleted"
        )


class ValidationTests(ServiceHarness):
    def test_validation(self) -> None:
        bad = [
            dict(name=""),
            dict(name=" lead"),
            dict(name="a/b"),
            dict(name="x" * 65),
            dict(labels={"Bad Key": "v"}),
            dict(labels={f"k{i}": "v" for i in range(11)}),
            dict(profile="huge"),
            dict(persistence="forever"),
            dict(network="host"),
            dict(idle_timeout_minutes=-1),
            dict(idle_timeout_minutes=10**7),
            dict(idempotency_key="short"),
        ]
        for extra in bad:
            kw = {"name": "ok name", "template_id": "terminal-ubuntu", **extra}
            self.assertEqual(code_of(lambda kw=kw: self.service.create(self.alice, **kw))[1], 422, extra)
        self.assertEqual(self.store.list(owner_id=None)[1], 0)

    def test_unknown_template_and_unavailable(self) -> None:
        self.assertEqual(code_of(lambda: self.make_ws(template="nope"))[1], 404)
        self.assertEqual(code_of(lambda: self.make_ws(template="custom-ghost"))[1], 404)
        # a streamed template whose image has not been built is unavailable, with a reason
        self.fake.images = set()
        self.assertEqual(
            code_of(lambda: self.make_ws(template="desktop-ubuntu")), ("template_unavailable", 409)
        )
        self.fake.images = None
        # Termux: container-only templates are unavailable and no docker adapter exists
        self.caps = make_caps({"TERMUX_VERSION": "1"}, docker=False, tmp=self.tmp)
        svc = self.new_service(runtimes={})
        self.assertEqual(
            code_of(lambda: svc.create(self.alice, name="t", template_id="terminal-ubuntu"))[0],
            "template_unavailable",
        )

    def test_authorized_use_acknowledgement(self) -> None:
        self.assertEqual(code_of(lambda: self.make_ws(template="security-lab"))[0], "confirmation_required")
        ws = self.make_ws(template="security-lab", acknowledge_authorized_use=True)
        self.assertEqual(self.status(ws), "running")

    def test_lab_capabilities_need_rules_and_come_from_the_offered_list(self) -> None:
        caps = ["NET_RAW", "NET_ADMIN"]
        # Rules not accepted: refused, whatever capabilities were asked for.
        self.assertEqual(
            code_of(lambda: self.make_ws(template="security-lab", capabilities=caps))[0],
            "confirmation_required",
        )
        # Capabilities outside the allowlist or not offered by this template are refused.
        for bad in (["SYS_ADMIN"], ["ALL"], ["NET_RAW", "SYS_MODULE"], ["net_raw"]):
            self.assertEqual(
                code_of(
                    lambda bad=bad: self.make_ws(
                        template="security-lab", acknowledge_authorized_use=True, capabilities=bad
                    )
                )[1],
                422,
                bad,
            )
        # A template that offers none refuses them even when the box is ticked.
        self.assertEqual(
            code_of(lambda: self.make_ws(template="dev-python", capabilities=["NET_RAW"]))[1], 422
        )
        ws = self.make_ws(
            template="security-lab", acknowledge_authorized_use=True, capabilities=caps, egress="direct"
        )
        self.assertEqual(ws.extra_caps, ("NET_RAW", "NET_ADMIN"))
        inst = self.store.current_instance(ws.id)
        spec = self.fake.resources[inst.resource_id].spec
        self.assertEqual(set(spec.capabilities), {"NET_RAW", "NET_ADMIN"})
        self.assertTrue(spec.run_as_root)
        events, _ = list_events(self.db, limit=50, action_prefix="workspace.")
        created = [e for e in events if e["action"].endswith("create") and e["outcome"] == "success"]
        details = str(created[0].get("details"))
        self.assertIn(LAB_RULES_VERSION, details)
        self.assertIn("NET_RAW", details)

    def test_tor_egress_defaults_and_rules(self) -> None:
        # Browsers and labs go through Tor unless the user explicitly chooses otherwise.
        ws = self.make_ws(template="browser-chromium", profile="medium")
        self.assertEqual(ws.egress, "tor")
        spec = self.fake.resources[self.store.current_instance(ws.id).resource_id].spec
        self.assertEqual(spec.egress, "tor")
        self.assertIn(ws.id, self.fake.ops("gateway"))
        direct = self.make_ws(name="two", template="browser-chromium", profile="medium", egress="direct")
        self.assertEqual(direct.egress, "direct")
        # Tor Browser has its own Tor: only direct is offered for it.
        bob = self.bob
        self.assertEqual(
            code_of(lambda: self.make_ws(bob, name="t", template="browser-tor", egress="tor"))[1], 422
        )
        self.assertEqual(self.make_ws(bob, name="t2", template="browser-tor").egress, "direct")
        # Terminals/dev tools default to direct but can opt in.
        self.assertEqual(self.make_ws(bob, name="d", template="dev-python").egress, "direct")
        admin = self.admin
        self.assertEqual(self.make_ws(admin, name="d2", template="dev-python", egress="tor").egress, "tor")
        # Nonsense values and the network "none" combination are refused.
        self.assertEqual(
            code_of(lambda: self.make_ws(admin, name="x", template="dev-python", egress="vpn"))[1], 422
        )
        self.assertEqual(
            code_of(
                lambda: self.make_ws(admin, name="y", template="dev-python", egress="tor", network="none")
            )[1],
            422,
        )

    def test_tor_refused_with_net_admin_and_without_gateway_image(self) -> None:
        self.assertEqual(
            code_of(
                lambda: self.make_ws(
                    template="security-lab",
                    acknowledge_authorized_use=True,
                    capabilities=["NET_ADMIN"],
                    egress="tor",
                )
            )[1],
            422,
        )
        # No gateway image: creation fails closed. It never silently falls back to a direct connection.
        self.fake.images = {"orbitnest/stream-chromium:1"}
        self.assertEqual(
            code_of(lambda: self.make_ws(name="g", template="browser-chromium", profile="medium"))[0],
            "template_unavailable",
        )
        self.assertEqual(self.store.list(owner_id=self.alice.id)[1], 0)

    def test_browsers_refuse_sizes_that_cannot_run_them(self) -> None:
        self.assertEqual(code_of(lambda: self.make_ws(template="browser-chromium", profile="small"))[1], 422)
        ws = self.make_ws(template="browser-chromium", profile="medium")
        self.assertEqual(ws.resource_profile, "medium")
        self.assertEqual(
            self.make_ws(name="dev two", template="dev-python", profile="small").resource_profile, "small"
        )

    def test_read_only_cannot_create(self) -> None:
        ro = self.add_user("viewer", "read_only")
        self.assertEqual(code_of(lambda: self.make_ws(ro))[1], 403)

    def test_idempotency(self) -> None:
        a, created_a = self.service.create(
            self.alice, name="one", template_id="dev-python", idempotency_key="key-12345678"
        )
        b, created_b = self.service.create(
            self.alice, name="one", template_id="dev-python", idempotency_key="key-12345678"
        )
        self.assertEqual((a.id, created_a, created_b), (b.id, True, False))
        self.assertEqual(self.store.list(owner_id=self.alice.id)[1], 1)
        c, created_c = self.service.create(
            self.bob, name="one", template_id="dev-python", idempotency_key="key-12345678"
        )
        self.assertNotEqual(c.id, a.id)
        self.assertTrue(created_c)

    def test_name_uniqueness_per_owner_case_insensitive(self) -> None:
        self.make_ws(name="Alpha")
        self.assertEqual(code_of(lambda: self.make_ws(name="alpha"))[0], "conflict")
        self.make_ws(self.bob, name="alpha")


class QuotaTests(ServiceHarness):
    def test_workspace_and_running_quotas(self) -> None:
        policy_mod.update(self.db, {"max_workspaces_per_user": 3, "max_running_per_user": 2})
        a = self.make_ws(name="a")
        b = self.make_ws(name="b")
        self.assertEqual(code_of(lambda: self.make_ws(name="c"))[0], "session_limit")
        self.service.stop(self.alice, b.id)
        c = self.make_ws(name="c")
        self.assertEqual(code_of(lambda: self.service.start(self.alice, b.id))[0], "session_limit")
        self.assertEqual(code_of(lambda: self.make_ws(name="d"))[0], "quota_exceeded")
        self.service.delete(self.alice, c.id)
        self.service.start(self.alice, b.id)
        self.assertEqual(self.status(a), "running")
        self.make_ws(self.bob, name="a")  # another user is unaffected

    def test_concurrent_creates_cannot_exceed_quota(self) -> None:
        self.runner = ManualRunner()
        self.service = self.new_service(runner=self.runner)
        policy_mod.update(self.db, {"max_workspaces_per_user": 3, "max_running_per_user": 50})
        results = self.concurrently(
            [
                (lambda i=i: self.service.create(self.alice, name=f"w{i}", template_id="dev-python"))
                for i in range(8)
            ]
        )
        ok = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(ok), 3)
        for kind, exc in (r for r in results if r[0] == "err"):
            self.assertIsInstance(exc, errors.AppError)
            self.assertEqual(exc.code, "quota_exceeded")
        self.assertEqual(self.store.count_owner(self.alice.id), 3)

    def test_concurrent_starts_cannot_exceed_running_limit(self) -> None:
        policy_mod.update(self.db, {"max_workspaces_per_user": 10, "max_running_per_user": 2})
        ids = []
        for i in range(4):
            ws = self.make_ws(name=f"w{i}") if i < 2 else None
            if ws:
                ids.append(ws.id)
        for i in ids:
            self.service.stop(self.alice, i)
        policy_mod.update(self.db, {"max_running_per_user": 1})
        results = self.concurrently([(lambda i=i: self.service.start(self.alice, i)) for i in ids])
        self.assertEqual(sum(r[0] == "ok" for r in results), 1)
        self.assertEqual(self.store.count_owner(self.alice.id, ["running", "starting", "provisioning"]), 1)


class AccessControlTests(ServiceHarness):
    def test_cross_user_access_is_404_and_audited(self) -> None:
        ws = self.make_ws()
        calls = [
            lambda: self.service.get(self.bob, ws.id),
            lambda: self.service.stop(self.bob, ws.id),
            lambda: self.service.start(self.bob, ws.id),
            lambda: self.service.restart(self.bob, ws.id),
            lambda: self.service.reset(self.bob, ws.id),
            lambda: self.service.delete(self.bob, ws.id),
            lambda: self.service.update(self.bob, ws.id, name="mine"),
            lambda: self.service.events(self.bob, ws.id),
            lambda: self.service.touch_activity(self.bob, ws.id),
        ]
        for call in calls:
            self.assertEqual(code_of(call), ("not_found", 404))
        self.assertEqual(code_of(lambda: self.service.get(self.bob, "01ARZ3NDEKTSV4RRFFQ69G5FAV"))[1], 404)
        self.assertEqual(code_of(lambda: self.service.get(self.bob, "../../etc"))[1], 404)
        self.assertEqual(self.status(ws), "running")
        with self.db.connect() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action = 'workspace.access_denied'"
            ).fetchone()[0]
        self.assertEqual(n, len(calls))

    def test_admin_access_and_listing(self) -> None:
        ws = self.make_ws()
        self.make_ws(self.bob, name="bobs")
        self.assertEqual(self.service.get(self.admin, ws.id).id, ws.id)
        self.service.stop(self.admin, ws.id)
        self.assertEqual(self.status(ws), "stopped")
        self.assertEqual([w.name for w in self.service.list(self.alice)[0]], ["dev one"])
        self.assertEqual(self.service.list(self.admin, all_users=True)[1], 2)
        self.assertEqual(code_of(lambda: self.service.list(self.alice, all_users=True))[1], 403)
        self.assertEqual(code_of(lambda: self.service.list(self.alice, status="weird"))[1], 422)

    def test_list_filters_and_search_escaping(self) -> None:
        self.make_ws(name="alpha_1")
        self.make_ws(name="alphaX1")
        self.assertEqual(self.service.list(self.alice, query="alpha_1")[1], 1)  # '_' is literal
        self.assertEqual(self.service.list(self.alice, query="%")[1], 0)
        self.assertEqual(self.service.list(self.alice, template_id="terminal-ubuntu")[1], 2)


class RaceTests(ServiceHarness):
    runner_cls = ManualRunner

    def test_abandoned_states_are_reconciled_at_boot(self) -> None:
        ws = self.make_ws()
        self.runner.drop_pending()  # crash before the provisioning job ran
        self.assertEqual(self.status(ws), "provisioning")
        fresh = self.new_service()
        report = fresh.reconcile(boot=True)
        self.assertEqual(self.status(ws), "failed")
        self.assertEqual(report.reconciled[0]["was"], "provisioning")
        fresh.start(self.alice, ws.id)
        self.runner.run_pending()
        self.assertEqual(self.status(ws), "running")

    def test_recent_transitions_are_left_alone_until_abandoned(self) -> None:
        ws = self.make_ws()
        self.runner.drop_pending()
        fresh = self.new_service()
        fresh.reconcile()
        self.assertEqual(self.status(ws), "provisioning")
        self.time.advance(60)
        fresh.reconcile()
        self.assertEqual(self.status(ws), "failed")

    def test_in_flight_job_is_not_reconciled(self) -> None:
        ws = self.make_ws()  # job queued => in flight in this process
        self.time.advance(600)
        self.service.reconcile()
        self.assertEqual(self.status(ws), "provisioning")
        self.runner.run_pending()
        self.assertEqual(self.status(ws), "running")

    def test_delete_during_provisioning_leaves_nothing_behind(self) -> None:
        deleted = []

        def hook(op: str, rid: str) -> None:
            if op == "create" and not deleted:
                deleted.append(1)
                self.service.delete(self.alice, ws.id)

        ws = self.make_ws()
        self.fake.hook = hook
        self.runner.run_pending()  # provisioning job: create() triggers the delete mid-flight
        self.runner.run_pending()  # the queued delete job
        self.assertEqual(self.status(ws), "deleted")
        self.assertEqual(self.fake.resources, {})
        self.assertEqual(self.store.live_instances(ws.id), [])

    def test_stale_job_cannot_resurrect_a_deleted_workspace(self) -> None:
        ws = self.make_ws()
        self.service.delete(self.alice, ws.id)
        self.runner.run_pending()  # stale provision job then the delete job, in queue order
        self.assertEqual(self.status(ws), "deleted")
        self.assertEqual(self.fake.resources, {})

    def test_abandoned_deleting_is_failed_not_purged(self) -> None:
        ws = self.make_ws()
        self.runner.run_pending()
        (self.service.storage.volume_dir(ws.id) / "f").write_text("x")
        self.service.delete(self.alice, ws.id, delete_data=True, confirm_name="dev one")
        self.runner.drop_pending()
        self.new_service().reconcile(boot=True)
        self.assertEqual(self.status(ws), "failed")
        self.assertTrue((self.service.storage.volume_dir(ws.id) / "f").exists())


class MaintenanceTests(ServiceHarness):
    def test_idle_marking_activity_and_timeout_stop(self) -> None:
        ws = self.make_ws(idle_timeout_minutes=60)
        self.time.advance(10)
        self.assertEqual(self.service.sweep_idle().marked_idle, [])
        self.time.advance(6)
        self.assertEqual(self.service.sweep_idle().marked_idle, [ws.id])
        self.assertEqual(self.status(ws), "idle")
        self.service.touch_activity(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")
        self.time.advance(61)
        self.assertEqual(self.service.sweep_idle().stopped_idle, [ws.id])
        self.assertEqual(self.status(ws), "stopped")

    def test_never_idle_and_mark_only_policy(self) -> None:
        a = self.make_ws(name="never", idle_timeout_minutes=0)
        policy_mod.update(self.db, {"idle_action": "mark"})
        b = self.make_ws(name="markonly", idle_timeout_minutes=5)
        self.time.advance(600)
        rep = self.service.sweep_idle()
        self.assertEqual(rep.stopped_idle, [])
        self.assertEqual({self.status(a), self.status(b)}, {"idle"})

    def test_runtime_lost_and_recovery(self) -> None:
        ws = self.make_ws()
        rid = self.store.current_instance(ws.id).resource_id
        self.fake.crash(rid, 137)
        self.service.reconcile()
        got = self.store.get(ws.id)
        self.assertEqual(got.status, "failed")
        self.assertIn("137", got.status_reason)
        self.service.start(self.alice, ws.id)
        self.assertEqual(self.status(ws), "running")
        self.fake.vanish(self.store.current_instance(ws.id).resource_id)
        self.service.reconcile()
        self.assertEqual(self.status(ws), "failed")
        self.assertIsNone(self.store.current_instance(ws.id))
        self.service.start(self.alice, ws.id)  # no instance: re-provisioned
        self.assertEqual(self.status(ws), "running")

    def test_unreadable_runtime_changes_nothing(self) -> None:
        ws = self.make_ws()
        self.fake.fail_next("inspect")
        self.service.reconcile()
        self.assertEqual(self.status(ws), "running")

    def test_orphans_dry_run_then_apply(self) -> None:
        ws = self.make_ws()
        spec = RuntimeSpec(
            WSID,
            "ubuntu:24.04",
            ("sleep", "infinity"),
            500,
            512,
            256,
            "none",
            "/home/workspace",
            labels={"orbitnest.workspace": WSID},
        )
        orphan = self.fake.adopt_orphan(spec)
        registered = self.store.current_instance(ws.id).resource_id
        dry = self.service.cleanup()
        self.assertEqual([o["resource_id"] for o in dry.orphans], [orphan])
        self.assertIn(orphan, self.fake.resources)
        done = self.service.cleanup(apply=True)
        self.assertEqual([o["resource_id"] for o in done.orphans_removed], [orphan])
        self.assertNotIn(orphan, self.fake.resources)
        self.assertIn(registered, self.fake.resources)

    def test_orphaned_tor_gateway_is_found_and_removed_but_a_live_one_is_kept(self) -> None:
        live = self.make_ws()
        self.fake.gateways["gw-live"] = {"orbitnest.workspace": live.id}
        self.fake.gateways["gw-gone"] = {"orbitnest.workspace": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
        found = self.service.find_orphans()
        self.assertEqual([o["resource_id"] for o in found], ["gw-gone"])
        self.assertIn("Tor gateway", found[0]["reason"])
        done = self.service.cleanup(apply=True)
        self.assertEqual([o["resource_id"] for o in done.orphans_removed], ["gw-gone"])
        self.assertEqual(list(self.fake.gateways), ["gw-live"])

    def test_registered_resource_of_deleted_workspace_is_orphan(self) -> None:
        ws = self.make_ws()
        rid = self.store.current_instance(ws.id).resource_id
        self.fake.hook = lambda op, r: (
            (_ for _ in ()).throw(__import__("app.runtimes.base", fromlist=["x"]).RuntimeFailure("no"))
            if op == "remove"
            else None
        )
        self.service.delete(self.alice, ws.id)
        self.assertEqual(self.status(ws), "failed")
        self.fake.hook = None
        with self.db.transaction() as c:
            c.execute("UPDATE workspaces SET status='deleted' WHERE id = ?", (ws.id,))
        found = self.service.find_orphans()
        self.assertEqual([o["resource_id"] for o in found], [rid])

    def test_data_dirs_reported_but_only_purged_ones_removed(self) -> None:
        ws = self.make_ws()
        self.service.delete(self.alice, ws.id)  # retained
        stray = self.service.storage.volume_dir("01ARZ3NDEKTSV4RRFFQ69G5FAV")
        stray.mkdir(parents=True)
        (stray / "x").write_text("x")
        self.store.set_volume_state(ws.id, "retained")
        rep = self.service.cleanup(apply=True)
        self.assertEqual(rep.retained_data, [ws.id])
        self.assertEqual([d["workspace_id"] for d in rep.leftover_data], ["01ARZ3NDEKTSV4RRFFQ69G5FAV"])
        self.assertTrue(stray.exists())  # unknown directories are never deleted
        self.assertTrue(self.service.storage.exists(ws.id))
        self.store.set_volume_state(ws.id, "purged")
        rep = self.service.cleanup(apply=True)
        self.assertEqual(rep.leftover_removed, [ws.id])
        self.assertFalse(self.service.storage.exists(ws.id))


if __name__ == "__main__":
    unittest.main()
