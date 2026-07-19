"""Tests for SQLite backup/restore operator tooling."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server.ops import backup


def _make_db(path: Path, value: str = "one"):
    conn = sqlite3.connect(os.fspath(path))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()


class PathTests(unittest.TestCase):
    def test_sqlite_path_from_url(self):
        self.assertEqual(
            backup.sqlite_path_from_url("sqlite:///x/y.db"), Path("x/y.db")
        )

    def test_rejects_non_file(self):
        with self.assertRaises(backup.BackupError):
            backup.sqlite_path_from_url("postgres://x")
        with self.assertRaises(backup.BackupError):
            backup.sqlite_path_from_url("sqlite:///:memory:")


class BackupTests(unittest.TestCase):
    def test_backup_creates_valid_copy(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "deepbox.db"
            _make_db(src)
            dest = backup.backup_database(f"sqlite:///{src.as_posix()}", Path(d) / "backups")
            self.assertTrue(dest.exists())
            self.assertTrue(backup.integrity_ok(dest))
            self.assertTrue(backup.is_sqlite_file(dest))

    def test_backup_missing_source(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(backup.BackupError):
                backup.backup_database(f"sqlite:///{d}/none.db", Path(d))


class RestoreTests(unittest.TestCase):
    def test_restore_swaps_and_preserves(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "deepbox.db"
            _make_db(live, "old")
            bak = Path(d) / "backup.db"
            _make_db(bak, "new")

            url = f"sqlite:///{live.as_posix()}"
            backup.restore_database(url, bak, force=True)

            conn = sqlite3.connect(os.fspath(live))
            self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "new")
            conn.close()
            # Pre-restore sidecar preserved the old DB.
            self.assertTrue((live.with_suffix(".db.pre-restore")).exists())

    def test_restore_rejects_non_sqlite(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "deepbox.db"
            _make_db(live)
            bad = Path(d) / "bad.db"
            bad.write_text("not a database")
            with self.assertRaises(backup.BackupError):
                backup.restore_database(f"sqlite:///{live.as_posix()}", bad)

    def test_restore_missing_backup(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "deepbox.db"
            _make_db(live)
            with self.assertRaises(backup.BackupError):
                backup.restore_database(f"sqlite:///{live.as_posix()}", Path(d) / "none.db")

    def test_restore_requires_explicit_force_for_existing_database(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "deepbox.db"
            _make_db(live, "old")
            bak = Path(d) / "backup.db"
            _make_db(bak, "new")
            url = f"sqlite:///{live.as_posix()}"

            with self.assertRaises(backup.BackupError):
                backup.restore_database(url, bak)

            # The operator stops the server before acknowledging replacement.
            backup.restore_database(url, bak, force=True)
            conn = sqlite3.connect(os.fspath(live))
            self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "new")
            conn.close()


class CliTests(unittest.TestCase):
    def test_cli_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "deepbox.db"
            _make_db(live)
            url = f"sqlite:///{live.as_posix()}"
            rc = backup.main(["--database-url", url, "backup", str(Path(d) / "b")])
            self.assertEqual(rc, 0)
            created = list((Path(d) / "b").glob("*.db"))
            self.assertEqual(len(created), 1)
            rc = backup.main(["--database-url", url, "restore", str(created[0]), "--force"])
            self.assertEqual(rc, 0)

    def test_cli_error_returns_1(self):
        rc = backup.main(["--database-url", "postgres://x", "backup", "out"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
