"""Tests for models._migrate additive migration + workspace backfill."""
import os
import sqlite3
import tempfile
import unittest
import uuid

from server.app import models
from server.app.models import (
    Devbox,
    Membership,
    Organization,
    Session,
    Workspace,
)


class NewDatabaseStartupTests(unittest.TestCase):
    def test_fresh_db_starts(self):
        with tempfile.TemporaryDirectory() as d:
            url = f"sqlite:///{os.path.join(d, 'fresh.db')}"
            engine = models.init_db(url)
            # migration is idempotent
            models._migrate(engine)
            self.assertTrue(engine is not None)
            engine.dispose()


class LegacyBackfillTests(unittest.TestCase):
    def _legacy_db(self, path):
        """Create a minimal legacy schema (no workspace_id / collab tables)."""
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE user (
                id TEXT PRIMARY KEY, username TEXT, password_hash TEXT,
                display_name TEXT, role TEXT, disabled_at TIMESTAMP,
                created_at TIMESTAMP
            );
            CREATE TABLE devbox (
                id TEXT PRIMARY KEY, owner_user_id TEXT, name TEXT,
                created_at TIMESTAMP, last_seen_at TIMESTAMP, capabilities JSON
            );
            CREATE TABLE agent (
                id TEXT PRIMARY KEY, devbox_id TEXT, handle TEXT,
                display_name TEXT, runtime TEXT, cwd TEXT, launch_cmd TEXT,
                presence TEXT, created_at TIMESTAMP
            );
            CREATE TABLE session (
                id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT,
                title TEXT, retention TEXT, created_at TIMESTAMP
            );
            CREATE TABLE keyboard_lease (
                id TEXT PRIMARY KEY, session_id TEXT UNIQUE,
                holder_user_id TEXT, acquired_at TIMESTAMP, expires_at TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO user VALUES ('u1','a','h','A','member',NULL,NULL)")
        conn.execute(
            "INSERT INTO user VALUES ('u2','b','h','B','member',NULL,NULL)")
        conn.execute(
            "INSERT INTO devbox VALUES ('d1','u1','box1',NULL,NULL,NULL)")
        conn.execute(
            "INSERT INTO devbox VALUES ('d2','u1','box2',NULL,NULL,NULL)")
        conn.execute(
            "INSERT INTO devbox VALUES ('d3','u2','box3',NULL,NULL,NULL)")
        conn.execute(
            "INSERT INTO agent VALUES ('a1','d1','h','A','mock',NULL,NULL,NULL,NULL)")
        conn.execute(
            "INSERT INTO session VALUES ('s1','u1','a1','T','30d',NULL)")
        conn.commit()
        conn.close()

    def test_backfill(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "legacy.db")
            self._legacy_db(path)
            engine = models.init_db(f"sqlite:///{path}")
            db = models.SessionLocal()
            try:
                # one personal org/workspace per owner (u1, u2)
                self.assertEqual(db.query(Organization).count(), 2)
                self.assertEqual(db.query(Workspace).count(), 2)
                # owner membership per workspace
                self.assertEqual(db.query(Membership).count(), 2)
                for m in db.query(Membership).all():
                    self.assertEqual(m.role, "owner")

                # devboxes backfilled; u1's two boxes share workspace
                d1 = db.query(Devbox).filter_by(id="d1").one()
                d2 = db.query(Devbox).filter_by(id="d2").one()
                d3 = db.query(Devbox).filter_by(id="d3").one()
                self.assertIsNotNone(d1.workspace_id)
                self.assertEqual(d1.workspace_id, d2.workspace_id)
                self.assertNotEqual(d1.workspace_id, d3.workspace_id)

                # session backfilled via agent -> devbox
                s1 = db.query(Session).filter_by(id="s1").one()
                self.assertEqual(s1.workspace_id, d1.workspace_id)

                lease_cols = {
                    row[1] for row in db.execute(
                        models.text("PRAGMA table_info(keyboard_lease)"))
                }
                self.assertIn("version", lease_cols)
            finally:
                db.close()
                engine.dispose()

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "legacy.db")
            self._legacy_db(path)
            engine = models.init_db(f"sqlite:///{path}")
            models._migrate(engine)
            models._migrate(engine)
            db = models.SessionLocal()
            try:
                self.assertEqual(db.query(Organization).count(), 2)
                self.assertEqual(db.query(Workspace).count(), 2)
                self.assertEqual(db.query(Membership).count(), 2)
            finally:
                db.close()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
