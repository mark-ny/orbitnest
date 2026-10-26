import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.db import Database, migrate
from app.services.backup import BackupError, create_backup, restore_backup


def make_settings(root: Path) -> Settings:
    return Settings.from_env({"ORBITNEST_ENV": "test", "ORBITNEST_DATA_DIR": str(root)})


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src = make_settings(self.root / "src")
        db = Database(self.src.db_path)
        migrate(db)
        with db.transaction() as c:
            c.execute("CREATE TABLE marker (v TEXT)")
            c.execute("INSERT INTO marker VALUES ('hello')")
        (self.src.data_dir / "secret.key").write_text("k" * 48)
        ws = self.src.workspaces_root / "ws1"
        ws.mkdir(parents=True)
        (ws / "note.txt").write_text("data")

    def test_roundtrip_with_workspaces(self) -> None:
        res = create_backup(self.src, self.root / "out", include_workspaces=True)
        self.assertEqual(oct(res.path.stat().st_mode & 0o777), "0o600")
        dst = make_settings(self.root / "dst")
        names = restore_backup(dst, res.path)
        self.assertEqual(names, ["orbitnest.sqlite3", "secret.key", "workspaces"])
        self.assertEqual((dst.workspaces_root / "ws1" / "note.txt").read_text(), "data")
        self.assertEqual(oct((dst.data_dir / "secret.key").stat().st_mode & 0o777), "0o600")
        with Database(dst.db_path).connect() as c:
            self.assertEqual(c.execute("SELECT v FROM marker").fetchone()[0], "hello")

    def test_workspaces_excluded_by_default(self) -> None:
        res = create_backup(self.src, self.root / "out")
        with tarfile.open(res.path) as t:
            self.assertFalse(any(n.startswith("workspaces") for n in t.getnames()))

    def test_restore_refuses_existing_database(self) -> None:
        res = create_backup(self.src, self.root / "out")
        with self.assertRaises(BackupError):
            restore_backup(self.src, res.path)

    def test_missing_database(self) -> None:
        with self.assertRaises(BackupError):
            create_backup(make_settings(self.root / "empty"), self.root / "out")

    def _evil(self, name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> Path:
        path = self.root / "evil.tar.gz"
        with tarfile.open(path, "w:gz") as t:
            good = tarfile.TarInfo("orbitnest.sqlite3")
            good.size = 0
            t.addfile(good, io.BytesIO(b""))
            bad = tarfile.TarInfo(name)
            bad.type = kind
            bad.linkname = link
            bad.size = 1 if kind == tarfile.REGTYPE else 0
            t.addfile(bad, io.BytesIO(b"x") if kind == tarfile.REGTYPE else None)
        return path

    def test_hostile_archives_rejected_and_nothing_written(self) -> None:
        cases = [
            ("../escape.txt", tarfile.REGTYPE, ""),
            ("/etc/escape", tarfile.REGTYPE, ""),
            ("workspaces/../../escape", tarfile.REGTYPE, ""),
            ("workspaces/link", tarfile.SYMTYPE, "/etc/passwd"),
            ("other.txt", tarfile.REGTYPE, ""),
            ("secret.key/extra", tarfile.REGTYPE, ""),
        ]
        for name, kind, link in cases:
            with self.subTest(name=name):
                dst = make_settings(self.root / "dst")
                with self.assertRaises(BackupError):
                    restore_backup(dst, self._evil(name, kind, link))
                self.assertFalse(dst.db_path.exists())
        self.assertFalse((self.root / "escape.txt").exists())

    def test_archive_without_database(self) -> None:
        path = self.root / "nodb.tar.gz"
        with tarfile.open(path, "w:gz") as t:
            i = tarfile.TarInfo("secret.key")
            i.size = 1
            t.addfile(i, io.BytesIO(b"x"))
        with self.assertRaises(BackupError):
            restore_backup(make_settings(self.root / "dst"), path)

    def test_not_an_archive(self) -> None:
        bad = self.root / "bad.tar.gz"
        bad.write_bytes(os.urandom(64))
        with self.assertRaises(BackupError):
            restore_backup(make_settings(self.root / "dst"), bad)


if __name__ == "__main__":
    unittest.main()
