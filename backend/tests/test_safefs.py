"""The file layer must never touch anything outside the workspace data directory."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.storage import safefs
from app.storage.safefs import FsError, SafeFs, split_path


def raises(code):
    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            assert et is FsError, f"expected FsError({code}), got {et}"
            assert ev.code == code, f"expected {code}, got {ev.code}"
            return True

    return Ctx()


class SplitPathTests(unittest.TestCase):
    def test_accepts_normal(self) -> None:
        self.assertEqual(split_path(""), [])
        self.assertEqual(split_path("/"), [])
        self.assertEqual(split_path("/a/b.txt"), ["a", "b.txt"])
        self.assertEqual(split_path("a/b/"), ["a", "b"])
        self.assertEqual(split_path("dir/файл ✓.txt"), ["dir", "файл ✓.txt"])

    def test_rejects_hostile(self) -> None:
        for bad in [
            "..",
            "a/../b",
            "a/./b",
            "a//b",
            "a\\b",
            "a\x00b",
            "a/\nb",
            "x" * 300,
            "/".join("d" * 1 for _ in range(40)),
            "a" * 1100,
            ".orbitnest-upload-abc",
            "a/\x7f",
            "\ud800",
        ]:
            with raises("invalid_path"):
                split_path(bad)

    def test_non_string(self) -> None:
        with raises("invalid_path"):
            split_path(None)  # type: ignore[arg-type]


class SafeFsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        base = Path(self._t.name)
        self.root = base / "data"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("TOP SECRET")
        self.fs = SafeFs(self.root)

    def tearDown(self) -> None:
        self._t.cleanup()

    def put(self, path: str, data: bytes = b"x", **kw) -> int:
        return self.fs.write_file(
            split_path(path),
            [data],
            max_bytes=kw.pop("max_bytes", 10**6),
            overwrite=kw.pop("overwrite", False),
        )

    # -- escapes ----------------------------------------------------------------------
    def test_symlinked_directory_is_never_followed(self) -> None:
        os.symlink(self.outside, self.root / "link")
        with raises("not_a_directory"):
            self.fs.list_dir(["link"])
        with raises("not_a_directory"):
            self.fs.open_read(["link", "secret.txt"])
        with raises("not_a_directory"):
            self.put("link/new.txt")
        with raises("not_a_directory"):
            self.fs.mkdir(["link", "d"])
        with raises("not_a_directory"):
            self.fs.delete(["link", "secret.txt"], recursive=False)
        self.assertEqual((self.outside / "secret.txt").read_text(), "TOP SECRET")
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])

    def test_symlinked_file_cannot_be_read_or_overwritten_through(self) -> None:
        os.symlink(self.outside / "secret.txt", self.root / "peek")
        with raises("not_a_file"):
            self.fs.open_read(["peek"])
        self.put("peek", b"pwned", overwrite=True)  # replaces the link itself, not its target
        self.assertEqual((self.outside / "secret.txt").read_text(), "TOP SECRET")
        self.assertFalse((self.root / "peek").is_symlink())
        self.assertEqual((self.root / "peek").read_bytes(), b"pwned")

    def test_absolute_symlink_and_dangling_and_chains(self) -> None:
        os.symlink("/etc", self.root / "etc")
        os.symlink("missing", self.root / "dangling")
        os.symlink("etc", self.root / "chain")
        for name in ("etc", "chain"):
            with raises("not_a_directory"):
                self.fs.list_dir([name])
        with raises("not_a_file"):
            self.fs.open_read(["dangling"])

    def test_delete_symlink_removes_link_not_target(self) -> None:
        os.symlink(self.outside, self.root / "link")
        self.fs.delete(["link"], recursive=True)
        self.assertFalse(os.path.lexists(self.root / "link"))
        self.assertTrue((self.outside / "secret.txt").exists())

    def test_recursive_delete_does_not_follow_nested_symlink(self) -> None:
        (self.root / "d" / "sub").mkdir(parents=True)
        os.symlink(self.outside, self.root / "d" / "sub" / "escape")
        (self.root / "d" / "f").write_text("x")
        self.fs.delete(["d"], recursive=True)
        self.assertFalse((self.root / "d").exists())
        self.assertEqual((self.outside / "secret.txt").read_text(), "TOP SECRET")

    def test_move_cannot_escape_or_follow(self) -> None:
        os.symlink(self.outside, self.root / "link")
        self.put("a.txt")
        with raises("not_a_directory"):
            self.fs.move(["a.txt"], ["link", "a.txt"])
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])

    def test_fifo_and_special_files_refused(self) -> None:
        os.mkfifo(self.root / "pipe")
        with raises("not_a_file"):
            self.fs.open_read(["pipe"])  # must return immediately, not block

    def test_listing_marks_symlinks_without_following(self) -> None:
        os.symlink(self.outside / "secret.txt", self.root / "peek")
        os.symlink(self.outside, self.root / "dirlink")
        entries, _ = self.fs.list_dir([])
        kinds = {e.name: e.kind for e in entries}
        self.assertEqual(kinds, {"peek": "symlink", "dirlink": "symlink"})
        self.assertTrue(all(e.size == 0 for e in entries))

    # -- behaviour --------------------------------------------------------------------
    def test_roundtrip_list_download_stat(self) -> None:
        self.fs.mkdir(["docs"])
        self.assertEqual(self.put("docs/a.txt", b"hello"), 5)
        entries, _truncated = self.fs.list_dir([])
        self.assertEqual([(e.name, e.kind) for e in entries], [("docs", "dir")])
        entries, _ = self.fs.list_dir(["docs"])
        self.assertEqual([(e.name, e.size) for e in entries], [("a.txt", 5)])
        fh, size, _ = self.fs.open_read(["docs", "a.txt"])
        with fh:
            self.assertEqual((fh.read(), size), (b"hello", 5))
        self.assertEqual(self.fs.stat(["docs", "a.txt"]).kind, "file")
        self.assertEqual(self.fs.stat([]).kind, "dir")
        with raises("not_found"):
            self.fs.stat(["nope"])

    def test_upload_rules(self) -> None:
        self.put("a.txt", b"one")
        with raises("exists"):
            self.put("a.txt", b"two")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one")
        self.put("a.txt", b"two", overwrite=True)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"two")
        self.fs.mkdir(["d"])
        with raises("is_a_directory"):
            self.put("d", overwrite=True)
        with raises("not_found"):
            self.put("missing/x.txt")

    def test_too_large_leaves_nothing_behind(self) -> None:
        with raises("too_large"):
            self.fs.write_file(["big"], [b"a" * 600, b"b" * 600], max_bytes=1000, overwrite=False)
        self.assertEqual(os.listdir(self.root), [])
        self.put("keep", b"old")
        with raises("too_large"):
            self.fs.write_file(["keep"], [b"z" * 2000], max_bytes=1000, overwrite=True)
        self.assertEqual((self.root / "keep").read_bytes(), b"old")  # original survives
        self.assertEqual(os.listdir(self.root), ["keep"])

    def test_failure_mid_stream_cleans_temp(self) -> None:
        def gen():
            yield b"abc"
            raise ConnectionResetError

        with self.assertRaises(ConnectionResetError):
            self.fs.write_file(["x"], gen(), max_bytes=100, overwrite=False)
        self.assertEqual(os.listdir(self.root), [])

    def test_temp_files_hidden_from_listing(self) -> None:
        (self.root / ".orbitnest-upload-abc").write_text("partial")
        self.assertEqual(self.fs.list_dir([])[0], [])

    def test_mkdir_move_delete(self) -> None:
        self.fs.mkdir(["a"])
        with raises("exists"):
            self.fs.mkdir(["a"])
        self.put("a/f", b"1")
        self.fs.move(["a"], ["b"])
        self.assertTrue((self.root / "b" / "f").exists())
        with raises("invalid_path"):
            self.fs.move(["b"], ["b", "inner"])
        with raises("invalid_path"):
            self.fs.move([], ["x"])
        self.put("c", b"2")
        with raises("exists"):
            self.fs.move(["c"], ["b"])
        with raises("not_found"):
            self.fs.move(["ghost"], ["g2"])
        with raises("not_empty"):
            self.fs.delete(["b"], recursive=False)
        self.fs.delete(["b"], recursive=True)
        self.fs.delete(["c"], recursive=False)
        with raises("forbidden"):
            self.fs.delete([], recursive=True)
        with raises("not_found"):
            self.fs.delete(["c"], recursive=False)
        self.assertEqual(os.listdir(self.root), [])

    def test_listing_is_bounded_and_sorted(self) -> None:
        for i in range(safefs.MAX_ENTRIES + 5):
            (self.root / f"f{i:05d}").write_text("")
        (self.root / "aaa-dir").mkdir()
        entries, truncated = self.fs.list_dir([])
        self.assertTrue(truncated)
        self.assertEqual(len(entries), safefs.MAX_ENTRIES)

    def test_missing_root(self) -> None:
        with raises("not_found"):
            SafeFs(self.root / "nope").list_dir([])

    def test_unsupported_platform_is_reported(self) -> None:
        from unittest import mock

        with mock.patch.object(safefs, "supported", return_value=False), raises("unsupported"):
            SafeFs(self.root)


if __name__ == "__main__":
    unittest.main()
