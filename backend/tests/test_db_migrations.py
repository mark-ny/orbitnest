from __future__ import annotations

import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from app.db import Database, MigrationError, migrate, migration_status
from app.db.migrations import Migration, discover


def mig(version: int, name: str, sql: str) -> Migration:
    import hashlib

    return Migration(version, name, sql, hashlib.sha256(sql.encode()).hexdigest())


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def tables(self) -> set[str]:
        with self.db.connect() as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def test_clean_database_migrates_to_latest(self) -> None:
        applied = migrate(self.db)
        self.assertEqual(applied, [m.version for m in discover()])
        self.assertTrue({"system_settings", "audit_events", "schema_migrations"} <= self.tables())
        self.assertTrue(migration_status(self.db).up_to_date)

    def test_second_run_is_noop(self) -> None:
        migrate(self.db)
        self.assertEqual(migrate(self.db), [])

    def test_status_reports_pending_before_migrating(self) -> None:
        status = migration_status(self.db)
        self.assertEqual(status.current, 0)
        self.assertFalse(status.up_to_date)

    def test_tampered_migration_refused(self) -> None:
        migrate(self.db, [mig(1, "a", "CREATE TABLE t (x TEXT) STRICT;")])
        with self.assertRaises(MigrationError) as ctx:
            migrate(self.db, [mig(1, "a", "CREATE TABLE t (x TEXT, y TEXT) STRICT;")])
        self.assertIn("modified", str(ctx.exception))

    def test_unknown_applied_migration_refused(self) -> None:
        migrate(self.db, [mig(1, "a", "CREATE TABLE t (x TEXT) STRICT;")])
        with self.assertRaises(MigrationError):
            migrate(self.db, [])

    def test_failed_migration_rolls_back_atomically(self) -> None:
        bad = mig(1, "bad", "CREATE TABLE ok_table (x TEXT) STRICT;\nINSERT INTO missing VALUES (1);")
        with self.assertRaises(MigrationError):
            migrate(self.db, [bad])
        self.assertNotIn("ok_table", self.tables())
        self.assertEqual(migration_status(self.db, [bad]).current, 0)

    def test_trigger_statements_split_correctly(self) -> None:
        sql = (
            "CREATE TABLE a (x INTEGER) STRICT;\n"
            "CREATE TABLE log (x INTEGER) STRICT;\n"
            "CREATE TRIGGER trg AFTER INSERT ON a BEGIN INSERT INTO log VALUES (NEW.x); END;\n"
        )
        migrate(self.db, [mig(1, "trig", sql)])
        with self.db.transaction() as c:
            c.execute("INSERT INTO a VALUES (?)", (7,))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT x FROM log").fetchone()[0], 7)

    def test_non_contiguous_versions_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "0002_x.sql").write_text("SELECT 1;")
            with self.assertRaises(MigrationError):
                discover(Path(d))

    def test_bad_filename_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "init.sql").write_text("SELECT 1;")
            with self.assertRaises(MigrationError):
                discover(Path(d))

    def test_foreign_keys_enforced_and_audit_outcome_check(self) -> None:
        migrate(self.db)
        with self.assertRaises(sqlite3.IntegrityError), self.db.transaction() as c:
            c.execute("INSERT INTO audit_events (id, action, outcome) VALUES (?, ?, ?)", ("1", "x", "bogus"))

    def test_transaction_rolls_back_on_error(self) -> None:
        migrate(self.db)
        with self.assertRaises(RuntimeError), self.db.transaction() as c:
            c.execute("INSERT INTO system_settings (key, value) VALUES ('k', 'v')")
            raise RuntimeError("boom")
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0], 0)

    @unittest.skipIf(sys.platform.startswith("win"), "POSIX permission bits")
    def test_database_file_is_owner_only(self) -> None:
        mode = stat.S_IMODE(self.db.path.stat().st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
