"""Tests for capacity evaluation logic."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.app import capacity


class ClassifyTests(unittest.TestCase):
    def test_growing_thresholds(self):
        self.assertEqual(capacity.classify_growing(10, 100, 200), "ok")
        self.assertEqual(capacity.classify_growing(150, 100, 200), "warn")
        self.assertEqual(capacity.classify_growing(250, 100, 200), "alert")
        self.assertEqual(capacity.classify_growing(100, 100, 200), "warn")

    def test_remaining_thresholds(self):
        self.assertEqual(capacity.classify_remaining(500, 100, 50), "ok")
        self.assertEqual(capacity.classify_remaining(80, 100, 50), "warn")
        self.assertEqual(capacity.classify_remaining(40, 100, 50), "alert")
        self.assertEqual(capacity.classify_remaining(50, 100, 50), "alert")

    def test_transition_events_are_edge_triggered(self):
        self.assertIsNone(capacity.transition_event("ok", "ok"))
        self.assertEqual(capacity.transition_event("ok", "warn"), "capacity.threshold")
        self.assertEqual(capacity.transition_event("warn", "alert"), "capacity.threshold")
        self.assertEqual(capacity.transition_event("alert", "ok"), "capacity.recovered")


class EvaluateTests(unittest.TestCase):
    def test_overall_worst_wins(self):
        report = capacity.evaluate_capacity(
            db_size_mb=10, disk_free_mb=40,
            db_size_warn_mb=100, db_size_alert_mb=200,
            disk_free_warn_mb=100, disk_free_alert_mb=50,
        )
        self.assertEqual(report.status, "alert")
        d = report.to_dict()
        self.assertEqual(d["status"], "alert")
        self.assertEqual(len(d["resources"]), 2)

    def test_all_ok(self):
        report = capacity.evaluate_capacity(
            db_size_mb=1, disk_free_mb=9999,
            db_size_warn_mb=100, db_size_alert_mb=200,
            disk_free_warn_mb=100, disk_free_alert_mb=50,
        )
        self.assertEqual(report.status, "ok")


class SqlitePathTests(unittest.TestCase):
    def test_file_url(self):
        self.assertEqual(capacity.sqlite_path("sqlite:///a/b.db"), Path("a/b.db"))

    def test_memory_and_other(self):
        self.assertIsNone(capacity.sqlite_path("sqlite:///:memory:"))
        self.assertIsNone(capacity.sqlite_path("postgres://x"))

    def test_database_size_mb(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.db"
            f.write_bytes(b"0" * (2 * 1024 * 1024))
            url = f"sqlite:///{f.as_posix()}"
            self.assertAlmostEqual(capacity.database_size_mb(url), 2.0, places=1)
        self.assertIsNone(capacity.database_size_mb("postgres://x"))


class CollectTests(unittest.TestCase):
    def test_collect(self):
        with tempfile.TemporaryDirectory() as d:
            dbf = Path(d) / "deepbox.db"
            dbf.write_bytes(b"0" * 1024)
            settings = SimpleNamespace(
                database_url=f"sqlite:///{dbf.as_posix()}",
                data_dir=Path(d),
                db_size_warn_mb=256.0, db_size_alert_mb=1024.0,
                disk_free_warn_mb=0.0, disk_free_alert_mb=0.0,
            )
            report = capacity.collect_capacity(settings)
            names = {r.name for r in report.resources}
            self.assertEqual(names, {"database", "recording_disk_free"})
            self.assertEqual(report.status, "ok")


if __name__ == "__main__":
    unittest.main()
