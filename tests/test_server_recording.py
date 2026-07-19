"""Server-side Protocol v3 durable output / ACK tests."""
import os
import tempfile
import unittest
import uuid

from server.app import models
from server.app.recording import (
    RecordingStore, NEW, DUPLICATE, GAP, CONFLICT, INVALID,
)
from server.app.live import LiveRegistry


def _frame(sid, pty, seq, data, kind="o", elapsed=None):
    f = {"type": "output", "session_id": sid, "pty_instance_id": pty,
         "seq": seq, "data": data, "kind": kind}
    if elapsed is not None:
        f["elapsed"] = elapsed
    return f


class RecordingBaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        models.init_db(f"sqlite:///{self._tmp.name}")
        self.db = models.SessionLocal()
        # Two devboxes, one agent each, one session on devbox A.
        self.uid = "u-" + uuid.uuid4().hex
        self.db.add(models.User(id=self.uid, username="a@b.c", password_hash="x",
                        display_name="A"))
        self.dbxA = "dbx-" + uuid.uuid4().hex
        self.dbxB = "dbx-" + uuid.uuid4().hex
        self.db.add(models.Devbox(id=self.dbxA, owner_user_id=self.uid, name="A"))
        self.db.add(models.Devbox(id=self.dbxB, owner_user_id=self.uid, name="B"))
        self.agA = "ag-" + uuid.uuid4().hex
        self.agB = "ag-" + uuid.uuid4().hex
        self.db.add(models.Agent(id=self.agA, devbox_id=self.dbxA, handle="a",
                          display_name="A"))
        self.db.add(models.Agent(id=self.agB, devbox_id=self.dbxB, handle="b",
                          display_name="B"))
        self.sid = "s-" + uuid.uuid4().hex
        self.db.add(models.Session(id=self.sid, user_id=self.uid, agent_id=self.agA,
                              title="S"))
        self.db.commit()
        self.store = RecordingStore()
        self.pty = "pty1"

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _count(self):
        return self.db.query(models.RecordingFrame).count()


class ProtocolVersionTests(unittest.TestCase):
    def test_version_is_three(self):
        self.assertEqual(models.PROTOCOL_VERSION, 3)


class PersistTests(RecordingBaseCase):
    def test_new_persists_and_is_ack_eligible(self):
        r = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, 1, "hi"))
        self.assertEqual(r.outcome, NEW)
        self.assertTrue(r.committed)
        self.assertEqual(self._count(), 1)

    def test_identical_duplicate_reacks_without_new_row(self):
        f = _frame(self.sid, self.pty, 1, "hi")
        self.assertEqual(self.store.persist_output(
            self.db, devbox_id=self.dbxA, frame=f).outcome, NEW)
        r = self.store.persist_output(self.db, devbox_id=self.dbxA, frame=f)
        self.assertEqual(r.outcome, DUPLICATE)
        self.assertTrue(r.committed)
        self.assertEqual(self._count(), 1)

    def test_conflicting_duplicate_rejected(self):
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "hi"))
        r = self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, self.pty, 1, "DIFFERENT"))
        self.assertEqual(r.outcome, CONFLICT)
        self.assertFalse(r.committed)
        self.assertEqual(self._count(), 1)

    def test_gap_returns_expected(self):
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "a"))
        r = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, 3, "c"))
        self.assertEqual(r.outcome, GAP)
        self.assertEqual(r.expected_seq, 2)
        self.assertEqual(self._count(), 1)

    def test_lower_seq_present_is_duplicate(self):
        # Below the frontier, an already-stored seq re-acks as a duplicate.
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "a"))
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 2, "b"))
        r = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, 1, "a"))
        self.assertEqual(r.outcome, DUPLICATE)

    def test_out_of_order_then_fill(self):
        self.assertEqual(self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, self.pty, 1, "a")).outcome, NEW)
        self.assertEqual(self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, self.pty, 3, "c")).outcome, GAP)
        self.assertEqual(self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, self.pty, 2, "b")).outcome, NEW)
        self.assertEqual(self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, self.pty, 3, "c")).outcome, NEW)

    def test_ownership_rejected_across_devboxes(self):
        r = self.store.persist_output(self.db, devbox_id=self.dbxB,
                                      frame=_frame(self.sid, self.pty, 1, "hi"))
        self.assertEqual(r.outcome, INVALID)
        self.assertEqual(self._count(), 0)

    def test_validation_rejections(self):
        for bad in [
            _frame("", self.pty, 1, "x"),
            _frame(self.sid, "", 1, "x"),
            _frame(self.sid, self.pty, 0, "x"),
            _frame(self.sid, self.pty, -1, "x"),
            {"type": "output", "session_id": "nope", "pty_instance_id": "p",
             "seq": 1, "data": "x"},
        ]:
            r = self.store.persist_output(self.db, devbox_id=self.dbxA, frame=bad)
            self.assertEqual(r.outcome, INVALID)

    def test_oversized_data_rejected(self):
        big = "x" * (RecordingStore().max_data_bytes + 1)
        r = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, 1, big))
        self.assertEqual(r.outcome, INVALID)

    def test_commit_occurs_before_ack_hook(self):
        seen = {}

        def hook(row):
            # Prove the row is durable at hook time: a fresh session can read it.
            other = models.SessionLocal()
            try:
                seen["visible"] = other.query(models.RecordingFrame).filter_by(
                    session_id=self.sid, seq=1).count()
            finally:
                other.close()

        store = RecordingStore(commit_hook=hook)
        r = store.persist_output(self.db, devbox_id=self.dbxA,
                                 frame=_frame(self.sid, self.pty, 1, "hi"))
        self.assertEqual(r.outcome, NEW)
        self.assertEqual(seen["visible"], 1)


class MergedRecordingTests(RecordingBaseCase):
    def test_durable_events_ordered_and_deterministic(self):
        for seq, data in [(1, "a"), (2, "b"), (3, "c")]:
            self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, seq, data,
                                                   elapsed=seq * 0.5))
        events = RecordingStore.durable_events(self.db, self.sid)
        self.assertEqual([e[2] for e in events], ["a", "b", "c"])
        times = [e[0] for e in events]
        self.assertEqual(times, sorted(times))

    def test_registry_merge_no_duplicates(self):
        for seq, data in [(1, "a"), (2, "b")]:
            self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, seq, data,
                                                   elapsed=seq * 0.5))
        reg = LiveRegistry(
            durable_loader=lambda s: RecordingStore.durable_events(
                models.SessionLocal(), s))
        merged = reg.merged_events(self.sid)
        datas = [m[2] for m in merged]
        self.assertEqual(datas.count("a"), 1)
        self.assertEqual(datas.count("b"), 1)

    def test_restore_after_registry_recreation(self):
        for seq, data in [(1, "hello "), (2, "world")]:
            self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, seq, data,
                                                   elapsed=seq * 0.5))

        def loader(s):
            d = models.SessionLocal()
            try:
                return RecordingStore.durable_events(d, s)
            finally:
                d.close()

        reg = LiveRegistry(durable_loader=loader)
        ls = reg.get_or_create(self.sid)
        self.assertIn("hello world", ls.restore_bytes())


if __name__ == "__main__":
    unittest.main()
