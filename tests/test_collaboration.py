import datetime as dt
import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.app import collaboration as collab
from server.app import models
from server.app.models import (
    Base,
    KeyboardLease,
    Membership,
    Organization,
    Workspace,
    WS_ROLE_ADMIN,
    WS_ROLE_OPERATOR,
    WS_ROLE_OWNER,
    WS_ROLE_VIEWER,
)


def _mkdb():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


class RoleOrderingTests(unittest.TestCase):
    def test_ranking_order(self):
        self.assertLess(collab.role_rank(WS_ROLE_VIEWER),
                        collab.role_rank(WS_ROLE_OPERATOR))
        self.assertLess(collab.role_rank(WS_ROLE_OPERATOR),
                        collab.role_rank(WS_ROLE_ADMIN))
        self.assertLess(collab.role_rank(WS_ROLE_ADMIN),
                        collab.role_rank(WS_ROLE_OWNER))

    def test_at_least(self):
        self.assertTrue(collab.role_at_least(WS_ROLE_ADMIN, WS_ROLE_OPERATOR))
        self.assertFalse(collab.role_at_least(WS_ROLE_VIEWER, WS_ROLE_OPERATOR))
        self.assertTrue(collab.role_at_least(WS_ROLE_OWNER, WS_ROLE_OWNER))

    def test_can_control(self):
        self.assertFalse(collab.can_control(WS_ROLE_VIEWER))
        self.assertTrue(collab.can_control(WS_ROLE_OPERATOR))
        self.assertTrue(collab.can_control(WS_ROLE_OWNER))

    def test_unknown_role(self):
        with self.assertRaises(ValueError):
            collab.role_rank("bogus")


class WorkspaceAccessTests(unittest.TestCase):
    def setUp(self):
        self.db = _mkdb()
        self.ws = Workspace(id="ws1", org_id="org1", name="W")
        self.db.add(Organization(id="org1", name="O"))
        self.db.add(self.ws)
        self.db.add(Membership(
            id=str(uuid.uuid4()), workspace_id="ws1",
            user_id="u_admin", role=WS_ROLE_ADMIN))
        self.db.add(Membership(
            id=str(uuid.uuid4()), workspace_id="ws1",
            user_id="u_view", role=WS_ROLE_VIEWER))
        self.db.commit()

    def test_get_role(self):
        self.assertEqual(collab.get_role(self.db, "ws1", "u_admin"), WS_ROLE_ADMIN)
        self.assertIsNone(collab.get_role(self.db, "ws1", "nobody"))

    def test_has_access(self):
        self.assertTrue(collab.has_workspace_access(
            self.db, "ws1", "u_admin", WS_ROLE_OPERATOR))
        self.assertFalse(collab.has_workspace_access(
            self.db, "ws1", "u_view", WS_ROLE_OPERATOR))
        self.assertFalse(collab.has_workspace_access(self.db, "ws1", "nobody"))

    def test_require_access(self):
        self.assertEqual(collab.require_workspace_access(
            self.db, "ws1", "u_admin", WS_ROLE_ADMIN), WS_ROLE_ADMIN)
        with self.assertRaises(collab.PermissionDenied):
            collab.require_workspace_access(
                self.db, "ws1", "u_view", WS_ROLE_OPERATOR)

    def test_list_workspaces(self):
        result = collab.list_user_workspaces(self.db, "u_admin")
        self.assertEqual([w.id for w in result], ["ws1"])
        self.assertEqual(collab.list_user_workspaces(self.db, "nobody"), [])


class KeyboardLeaseTests(unittest.TestCase):
    def setUp(self):
        self.db = _mkdb()
        self.t0 = dt.datetime(2024, 1, 1, 12, 0, 0)

    def test_expiration_compares_sqlite_naive_to_aware_utc(self):
        lease = KeyboardLease(
            session_id="s-aware", holder_user_id="u1",
            expires_at=self.t0, version=1)
        aware_now = self.t0.replace(tzinfo=dt.timezone.utc)
        self.assertTrue(collab.lease_is_expired(lease, aware_now))

    def test_expiration_normalizes_aware_offsets(self):
        offset = dt.timezone(dt.timedelta(hours=8))
        lease = KeyboardLease(
            session_id="s-offset", holder_user_id="u1",
            expires_at=(self.t0 + dt.timedelta(hours=8)).replace(tzinfo=offset),
            version=1)
        self.assertTrue(collab.lease_is_expired(
            lease, self.t0.replace(tzinfo=dt.timezone.utc)))

    def test_viewer_cannot_acquire(self):
        with self.assertRaises(collab.PermissionDenied):
            collab.acquire_keyboard_lease(
                self.db, "s1", "u1", WS_ROLE_VIEWER, now=self.t0)

    def test_acquire_new(self):
        lease = collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=self.t0)
        self.assertEqual(lease.holder_user_id, "u1")
        self.assertEqual(lease.expires_at, self.t0 + dt.timedelta(seconds=30))

    def test_unique_holder_conflict(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=self.t0)
        with self.assertRaises(collab.LeaseConflict):
            collab.acquire_keyboard_lease(
                self.db, "s1", "u2", WS_ROLE_ADMIN,
                ttl=dt.timedelta(seconds=30),
                now=self.t0 + dt.timedelta(seconds=10))

    def test_same_holder_renew(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=self.t0)
        later = self.t0 + dt.timedelta(seconds=10)
        lease = collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=later)
        self.assertEqual(lease.holder_user_id, "u1")
        self.assertEqual(lease.expires_at, later + dt.timedelta(seconds=30))
        self.assertEqual(self.db.query(KeyboardLease).count(), 1)

    def test_expired_preemption(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=self.t0)
        after = self.t0 + dt.timedelta(seconds=60)
        lease = collab.acquire_keyboard_lease(
            self.db, "s1", "u2", WS_ROLE_ADMIN,
            ttl=dt.timedelta(seconds=30), now=after)
        self.assertEqual(lease.holder_user_id, "u2")
        self.assertEqual(self.db.query(KeyboardLease).count(), 1)

    def test_renew_other_holder_conflict(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=self.t0)
        with self.assertRaises(collab.LeaseConflict):
            collab.renew_keyboard_lease(
                self.db, "s1", "u2", now=self.t0 + dt.timedelta(seconds=1))

    def test_renew_expired_error(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR,
            ttl=dt.timedelta(seconds=30), now=self.t0)
        with self.assertRaises(collab.LeaseError):
            collab.renew_keyboard_lease(
                self.db, "s1", "u1", now=self.t0 + dt.timedelta(seconds=60))

    def test_renew_missing(self):
        with self.assertRaises(collab.LeaseError):
            collab.renew_keyboard_lease(self.db, "sX", "u1", now=self.t0)

    def test_handoff(self):
        lease = collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR, now=self.t0)
        old_version = lease.version
        handed = collab.handoff_keyboard_lease(
            self.db, "s1", "u1", "u2", WS_ROLE_ADMIN,
            ttl=dt.timedelta(seconds=30), now=self.t0 + dt.timedelta(seconds=5))
        self.assertEqual(handed.holder_user_id, "u2")
        self.assertEqual(handed.version, old_version + 1)
        self.assertEqual(handed.expires_at, self.t0 + dt.timedelta(seconds=35))

    def test_handoff_rejects_viewer_target(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR, now=self.t0)
        with self.assertRaises(collab.PermissionDenied):
            collab.handoff_keyboard_lease(
                self.db, "s1", "u1", "u2", WS_ROLE_VIEWER, now=self.t0)

    def test_handoff_rejects_non_holder(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR, now=self.t0)
        with self.assertRaises(collab.LeaseConflict):
            collab.handoff_keyboard_lease(
                self.db, "s1", "u2", "u3", WS_ROLE_OPERATOR, now=self.t0)

    def test_release(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR, now=self.t0)
        self.assertTrue(collab.release_keyboard_lease(self.db, "s1", "u1"))
        self.assertIsNone(collab.get_keyboard_lease(self.db, "s1"))
        self.assertFalse(collab.release_keyboard_lease(self.db, "s1", "u1"))

    def test_release_other_holder_conflict(self):
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR, now=self.t0)
        with self.assertRaises(collab.LeaseConflict):
            collab.release_keyboard_lease(self.db, "s1", "u2")

    def test_get_lease(self):
        self.assertIsNone(collab.get_keyboard_lease(self.db, "s1"))
        collab.acquire_keyboard_lease(
            self.db, "s1", "u1", WS_ROLE_OPERATOR, now=self.t0)
        self.assertIsNotNone(collab.get_keyboard_lease(self.db, "s1"))


if __name__ == "__main__":
    unittest.main()
