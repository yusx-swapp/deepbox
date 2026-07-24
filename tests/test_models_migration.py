"""Tests for models._migrate additive migration + workspace backfill."""
import datetime as dt
import os
import sqlite3
import tempfile
import unittest
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.app import models
from server.app.models import (
    Devbox,
    Membership,
    Organization,
    Session,
    User,
    Workspace,
    WorkspaceInvitation,
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

    def test_personal_singletons_and_open_invites_are_unique(self):
        with tempfile.TemporaryDirectory() as d:
            url = f"sqlite:///{os.path.join(d, 'unique.db')}"
            engine = models.init_db(url)
            with models.SessionLocal() as db:
                db.add(User(
                    id="u1", username="owner", password_hash="hash",
                    display_name="Owner", role="owner"))
                db.commit()
                db.add(Organization(
                    id="o1", name="Personal", is_personal=True,
                    owner_user_id="u1"))
                db.commit()

                db.add(Organization(
                    id="o2", name="Duplicate", is_personal=True,
                    owner_user_id="u1"))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

                db.add(Workspace(
                    id="w1", org_id="o1", name="Personal",
                    is_personal=True))
                db.commit()
                db.add(Workspace(
                    id="w2", org_id="o1", name="Duplicate",
                    is_personal=True))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

                expiry = models.now() + dt.timedelta(days=1)
                db.add(WorkspaceInvitation(
                    id="i1", workspace_id="w1", email="member@example.com",
                    role="viewer", token_hash="hash-1", token_preview="one",
                    created_by_user_id="u1", expires_at=expiry))
                db.commit()
                db.add(WorkspaceInvitation(
                    id="i2", workspace_id="w1", email="member@example.com",
                    role="operator", token_hash="hash-2", token_preview="two",
                    created_by_user_id="u1", expires_at=expiry))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

                first = db.get(WorkspaceInvitation, "i1")
                first.revoked_at = models.now()
                db.commit()
                db.add(WorkspaceInvitation(
                    id="i3", workspace_id="w1", email="member@example.com",
                    role="operator", token_hash="hash-3", token_preview="tri",
                    created_by_user_id="u1", expires_at=expiry))
                db.commit()
                open_invites = db.scalars(select(WorkspaceInvitation).where(
                    WorkspaceInvitation.workspace_id == "w1",
                    WorkspaceInvitation.email == "member@example.com",
                    WorkspaceInvitation.accepted_at.is_(None),
                    WorkspaceInvitation.revoked_at.is_(None),
                )).all()
                self.assertEqual(["i3"], [item.id for item in open_invites])
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

                agent_cols = {
                    row[1] for row in db.execute(
                        models.text("PRAGMA table_info(agent)"))
                }
                self.assertIn("local_project_id", agent_cols)
                self.assertIn("runtime_config", agent_cols)
                devbox_cols = {
                    row[1] for row in db.execute(
                        models.text("PRAGMA table_info(devbox)"))
                }
                self.assertIn("skills", devbox_cols)
                project_table = db.execute(models.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='devbox_project'"
                )).first()
                self.assertIsNotNone(project_table)
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
