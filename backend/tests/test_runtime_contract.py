"""Behaviour every runtime adapter must satisfy. Run against the fake and the real local runtime."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtimes.base import RuntimeFailure, RuntimeSpecError
from app.runtimes.fake import FakeRuntime
from app.runtimes.termux_runtime import LocalProcessRuntime
from tests.runtime_helpers import make_spec


class ContractMixin:
    def make_runtime(self):  # pragma: no cover
        raise NotImplementedError

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.rt = self.make_runtime()
        super().setUp()

    def tearDown(self) -> None:
        for rid in list(self.rt.list_managed()):
            self.rt.remove(rid)
        self._tmp.cleanup()
        super().tearDown()

    def spec(self, **kw):
        return make_spec(str(self.data), **kw)

    def test_lifecycle(self) -> None:
        rid = self.rt.create(self.spec())
        self.assertEqual(self.rt.inspect(rid).state, "created")
        self.rt.start(rid)
        self.assertEqual(self.rt.inspect(rid).state, "running")
        self.rt.stop(rid, 2)
        self.assertEqual(self.rt.inspect(rid).state, "exited")
        self.rt.start(rid)
        self.assertEqual(self.rt.inspect(rid).state, "running")
        self.rt.remove(rid)
        self.assertEqual(self.rt.inspect(rid).state, "missing")

    def test_idempotent_stop_and_remove(self) -> None:
        rid = self.rt.create(self.spec())
        self.rt.stop(rid, 1)
        self.rt.stop(rid, 1)
        self.rt.remove(rid)
        self.rt.remove(rid)

    def test_start_twice_is_harmless(self) -> None:
        rid = self.rt.create(self.spec())
        self.rt.start(rid)
        self.rt.start(rid)
        self.assertEqual(self.rt.inspect(rid).state, "running")

    def test_list_managed_tracks_resources(self) -> None:
        self.assertEqual(self.rt.list_managed(), {})
        rid = self.rt.create(self.spec())
        listed = self.rt.list_managed()
        self.assertEqual(set(listed), {rid})
        self.assertEqual(listed[rid]["orbitnest.workspace"], self.spec().workspace_id)
        self.rt.remove(rid)
        self.assertEqual(self.rt.list_managed(), {})

    def test_invalid_specs_rejected(self) -> None:
        bad = [
            dict(image="--privileged"),
            dict(network="host"),
            dict(capabilities=("SYS_ADMIN",)),
            dict(mount_path="/"),
            dict(mount_path="/a,b"),
            dict(memory_mb=10**6),
            dict(workspace_id="not-a-ulid"),
            dict(command=("--rm",)),
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(RuntimeSpecError):
                self.rt.create(self.spec(**kw))

    def test_unknown_resource_is_reported_missing_not_raised(self) -> None:
        self.assertEqual(self.rt.inspect(self.unknown_id()).state, "missing")


class FakeContractTests(ContractMixin, unittest.TestCase):
    def make_runtime(self):
        return FakeRuntime()

    def unknown_id(self):
        return "fake-9999"

    def test_failure_injection(self) -> None:
        rid = self.rt.create(self.spec())
        self.rt.fail_next("start")
        with self.assertRaises(RuntimeFailure):
            self.rt.start(rid)
        self.rt.start(rid)
        self.assertEqual(self.rt.inspect(rid).state, "running")


class LocalContractTests(ContractMixin, unittest.TestCase):
    def make_runtime(self):
        return LocalProcessRuntime(self.tmp / "state")

    def unknown_id(self):
        return "local-doesnotexist-1"

    def test_isolation_is_declared_none(self) -> None:
        self.assertEqual(self.rt.isolation, "none")
        self.assertEqual(self.rt.name, "local")


if __name__ == "__main__":
    unittest.main()
