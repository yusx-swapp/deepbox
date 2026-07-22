"""Server-side Protocol v3 durable output / ACK tests."""
import os
import tempfile
import unittest
import uuid

from server.app import models
from server.app.recording import (
    RecordingStore, NEW, DUPLICATE, GAP, CONFLICT, INVALID,
    ErasureResult, PersistResult, output_ack_response,
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
        # Raw foreign-key IDs do not establish ORM dependency edges, so flush
        # the parent rows before inserting the child while FK checks are on.
        self.db.flush()
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


class ReadRangeAndMetadataTests(RecordingBaseCase):
    def _seed(self, n=5):
        for i in range(1, n + 1):
            self.store.persist_output(
                self.db, devbox_id=self.dbxA,
                frame=_frame(self.sid, self.pty, i, f"d{i}", elapsed=float(i)))

    def test_read_range_orders_by_frame_id(self):
        self._seed(3)
        rows = self.store.read_range(self.db, self.sid)
        self.assertEqual([r.seq for r in rows], [1, 2, 3])
        ids = [r.id for r in rows]
        self.assertEqual(ids, sorted(ids))

    def test_read_range_after_cursor(self):
        self._seed(4)
        rows = self.store.read_range(self.db, self.sid)
        after = rows[1].id
        tail = self.store.read_range(self.db, self.sid, after_frame_id=after)
        self.assertEqual([r.seq for r in tail], [3, 4])

    def test_read_range_limit(self):
        self._seed(5)
        rows = self.store.read_range(self.db, self.sid, limit=2)
        self.assertEqual(len(rows), 2)

    def test_metadata_summary(self):
        self._seed(3)
        meta = self.store.metadata(self.db, self.sid)
        self.assertEqual(meta["frame_count"], 3)
        self.assertEqual(meta["redacted_count"], 0)
        self.assertEqual(meta["pty_instance_ids"], [self.pty])
        self.assertIsNotNone(meta["first_frame_id"])
        self.assertIsNotNone(meta["last_frame_id"])

    def test_delete_removes_frames_and_checkpoints(self):
        self._seed(3)
        rows = self.store.read_range(self.db, self.sid)
        self.store.checkpoint(self.db, self.sid, frame_id=rows[-1].id,
                              screen="X", event_index=3)
        n = self.store.delete(self.db, self.sid)
        self.assertEqual(n, 3)
        self.assertEqual(self._count(), 0)
        self.assertEqual(self.store.checkpoints(self.db, self.sid), [])


class CheckpointTests(RecordingBaseCase):
    def _seed(self, n):
        rows = []
        for i in range(1, n + 1):
            r = self.store.persist_output(
                self.db, devbox_id=self.dbxA,
                frame=_frame(self.sid, self.pty, i, f"d{i}", elapsed=float(i)))
            rows.append(r.frame)
        return rows

    def test_checkpoint_idempotent_per_frame(self):
        rows = self._seed(2)
        c1 = self.store.checkpoint(self.db, self.sid, frame_id=rows[-1].id,
                                   screen="A", event_index=2)
        c2 = self.store.checkpoint(self.db, self.sid, frame_id=rows[-1].id,
                                   screen="B", event_index=2)
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(c2.screen, "B")
        self.assertEqual(len(self.store.checkpoints(self.db, self.sid)), 1)

    def test_latest_checkpoint_at_cursor(self):
        rows = self._seed(4)
        self.store.checkpoint(self.db, self.sid, frame_id=rows[0].id, screen="1")
        self.store.checkpoint(self.db, self.sid, frame_id=rows[2].id, screen="3")
        cp = self.store.latest_checkpoint(self.db, self.sid,
                                          at_frame_id=rows[1].id)
        self.assertEqual(cp.frame_id, rows[0].id)
        cp2 = self.store.latest_checkpoint(self.db, self.sid)
        self.assertEqual(cp2.frame_id, rows[2].id)

    def test_maybe_checkpoint_cadence(self):
        store = RecordingStore(checkpoint_interval=3)
        made = []
        for i in range(1, 7):
            r = store.persist_output(
                self.db, devbox_id=self.dbxA,
                frame=_frame(self.sid, self.pty, i, f"d{i}", elapsed=float(i)))
            cp = store.maybe_checkpoint(self.db, self.sid, frame=r.frame,
                                        screen_fn=lambda i=i: f"screen{i}")
            if cp:
                made.append(cp.event_index)
        self.assertEqual(made, [3, 6])

    def test_multiple_pty_streams_disambiguated(self):
        # Two interleaved pty streams: checkpoint frame_id is unambiguous.
        r1 = self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, "ptyA", 1, "a1"))
        r2 = self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(self.sid, "ptyB", 1, "b1"))
        self.assertNotEqual(r1.frame.id, r2.frame.id)
        cp = self.store.checkpoint(self.db, self.sid, frame_id=r2.frame.id,
                                   screen="S", event_index=2)
        tail = self.store.read_range(self.db, self.sid, after_frame_id=cp.frame_id)
        self.assertEqual(tail, [])


class RetentionTests(RecordingBaseCase):
    def _seed_old(self, n=3, days_old=40):
        import datetime as dt
        old = models.now() - dt.timedelta(days=days_old)
        rows = []
        for i in range(1, n + 1):
            r = self.store.persist_output(
                self.db, devbox_id=self.dbxA,
                frame=_frame(self.sid, self.pty, i, f"secret{i}", elapsed=float(i)))
            rows.append(r.frame.id)
        for fid in rows:
            f = self.db.get(models.RecordingFrame, fid)
            f.created_at = old
        self.db.commit()
        return [self.db.get(models.RecordingFrame, fid) for fid in rows]

    def test_permanent_never_redacts(self):
        sess = self.db.get(models.Session, self.sid)
        sess.retention = models.RETENTION_PERMANENT
        self.db.commit()
        self._seed_old()
        n = self.store.redact_expired(self.db)
        self.assertEqual(n, 0)

    def test_30d_redacts_old_payload_keeps_ledger(self):
        sess = self.db.get(models.Session, self.sid)
        sess.retention = models.RETENTION_30D
        self.db.commit()
        rows = self._seed_old(days_old=40)
        seqs_before = [r.seq for r in rows]
        hashes_before = [r.payload_hash for r in rows]
        n = self.store.redact_expired(self.db)
        self.assertEqual(n, 3)
        got = self.db.query(models.RecordingFrame).order_by(
            models.RecordingFrame.seq).all()
        # Ledger identity preserved.
        self.assertEqual([r.seq for r in got], seqs_before)
        self.assertEqual([r.payload_hash for r in got], hashes_before)
        # Payload redacted.
        for r in got:
            self.assertEqual(r.data, "")
            self.assertIsNotNone(r.redacted_at)

    def test_30d_keeps_recent(self):
        sess = self.db.get(models.Session, self.sid)
        sess.retention = models.RETENTION_30D
        self.db.commit()
        self._seed_old(days_old=3)
        n = self.store.redact_expired(self.db)
        self.assertEqual(n, 0)

    def test_none_redacts_immediately(self):
        sess = self.db.get(models.Session, self.sid)
        sess.retention = models.RETENTION_NONE
        self.db.commit()
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "x"))
        n = self.store.redact_expired(self.db)
        self.assertEqual(n, 0)  # persist_output already enforced the policy
        self.assertIsNotNone(self.db.query(models.RecordingFrame).one().redacted_at)

    def test_redacted_excluded_from_durable_events(self):
        sess = self.db.get(models.Session, self.sid)
        sess.retention = models.RETENTION_NONE
        self.db.commit()
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "topsecret"))
        self.store.redact_expired(self.db)
        events = RecordingStore.durable_events(self.db, self.sid)
        self.assertEqual(events, [])
        rows = self.store.read_range(self.db, self.sid)
        self.assertEqual(rows, [])

    def test_set_retention_validates_persists_and_enforces(self):
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "erase"))
        sess = self.db.get(models.Session, self.sid)
        redacted = self.store.set_retention(
            self.db, sess, models.RETENTION_NONE)
        self.assertEqual(redacted, 1)
        self.assertEqual(self.db.get(models.Session, self.sid).retention,
                         models.RETENTION_NONE)
        self.assertEqual(self.db.query(models.RecordingFrame).one().data, "")

    def test_set_retention_rejects_invalid_policy(self):
        sess = self.db.get(models.Session, self.sid)
        with self.assertRaises(ValueError):
            self.store.set_retention(self.db, sess, "forever-ish")
        self.assertEqual(sess.retention, models.RETENTION_30D)

    def test_none_policy_redacts_new_payload_but_keeps_duplicate_ledger(self):
        sess = self.db.get(models.Session, self.sid)
        self.store.set_retention(self.db, sess, models.RETENTION_NONE)
        committed = []
        self.store.commit_hook = lambda row: committed.append(
            (row.data, row.redacted_at))
        frame = _frame(self.sid, self.pty, 1, "secret")
        first = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                          frame=frame)
        self.assertEqual(committed[0][0], "")
        self.assertIsNotNone(committed[0][1])
        self.assertEqual(first.outcome, NEW)
        self.assertEqual(first.frame.data, "")
        self.assertIsNotNone(first.frame.redacted_at)
        duplicate = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                              frame=frame)
        self.assertEqual(duplicate.outcome, DUPLICATE)

    def test_redacted_duplicate_still_acks(self):
        # Duplicate-ACK semantics must survive redaction: same seq re-sent maps
        # to DUPLICATE (identity row preserved), not CONFLICT/NEW.
        sess = self.db.get(models.Session, self.sid)
        sess.retention = models.RETENTION_NONE
        self.db.commit()
        self.store.persist_output(self.db, devbox_id=self.dbxA,
                                  frame=_frame(self.sid, self.pty, 1, "dup"))
        self.store.redact_expired(self.db)
        r = self.store.persist_output(self.db, devbox_id=self.dbxA,
                                      frame=_frame(self.sid, self.pty, 1, "dup"))
        self.assertEqual(r.outcome, DUPLICATE)


class SecureEraseTests(RecordingBaseCase):
    def _seed(self, n=4):
        rows = []
        for i in range(1, n + 1):
            r = self.store.persist_output(
                self.db, devbox_id=self.dbxA,
                frame=_frame(self.sid, self.pty, i, f"secret{i}",
                             elapsed=float(i)))
            rows.append(r.frame)
        return rows

    def test_redacts_all_payloads_preserving_ledger(self):
        rows = self._seed(3)
        seqs_before = [r.seq for r in rows]
        hashes_before = [r.payload_hash for r in rows]
        ptys_before = [r.pty_instance_id for r in rows]
        ids_before = [r.id for r in rows]

        res = self.store.secure_erase(self.db, self.sid)
        self.assertIsInstance(res, ErasureResult)
        self.assertEqual(res.frame_count, 3)
        self.assertEqual(res.newly_redacted, 3)
        self.assertEqual(res.already_redacted, 0)

        got = self.db.query(models.RecordingFrame).order_by(
            models.RecordingFrame.seq).all()
        # Rows preserved (not deleted).
        self.assertEqual(self._count(), 3)
        self.assertEqual([r.id for r in got], ids_before)
        # Ledger identity preserved: seq, pty_instance_id and payload_hash.
        self.assertEqual([r.seq for r in got], seqs_before)
        self.assertEqual([r.payload_hash for r in got], hashes_before)
        self.assertEqual([r.pty_instance_id for r in got], ptys_before)
        # Payload discarded, redaction stamped.
        for r in got:
            self.assertEqual(r.data, "")
            self.assertIsNotNone(r.redacted_at)

    def test_deletes_all_checkpoints_and_none_remain(self):
        rows = self._seed(4)
        self.store.checkpoint(self.db, self.sid, frame_id=rows[1].id,
                              screen="SCREEN-A", event_index=2)
        self.store.checkpoint(self.db, self.sid, frame_id=rows[3].id,
                              screen="SCREEN-B", event_index=4)
        self.assertEqual(len(self.store.checkpoints(self.db, self.sid)), 2)

        res = self.store.secure_erase(self.db, self.sid)
        self.assertEqual(res.checkpoints_deleted, 2)
        # No checkpoint screen remains for the session.
        self.assertEqual(self.store.checkpoints(self.db, self.sid), [])
        self.assertIsNone(self.store.latest_checkpoint(self.db, self.sid))
        self.assertEqual(
            self.db.query(models.RecordingCheckpoint).filter_by(
                session_id=self.sid).count(), 0)

    def test_idempotent_second_call_redacts_nothing_new(self):
        self._seed(3)
        first = self.store.secure_erase(self.db, self.sid)
        self.assertEqual(first.newly_redacted, 3)
        stamps = [r.redacted_at for r in self.db.query(
            models.RecordingFrame).order_by(models.RecordingFrame.seq).all()]

        second = self.store.secure_erase(self.db, self.sid)
        self.assertEqual(second.frame_count, 3)
        self.assertEqual(second.newly_redacted, 0)
        self.assertEqual(second.already_redacted, 3)
        self.assertEqual(second.redacted_count, 3)
        self.assertEqual(second.checkpoints_deleted, 0)
        # Existing redaction timestamps are untouched by the repeat call.
        stamps_after = [r.redacted_at for r in self.db.query(
            models.RecordingFrame).order_by(models.RecordingFrame.seq).all()]
        self.assertEqual(stamps_after, stamps)

    def test_dedup_identity_preserved_duplicate_still_acks(self):
        # After erasure, re-sending an identical frame must map to DUPLICATE
        # (identity row preserved), never CONFLICT or NEW.
        frame = _frame(self.sid, self.pty, 1, "dup-payload")
        self.assertEqual(self.store.persist_output(
            self.db, devbox_id=self.dbxA, frame=frame).outcome, NEW)
        self.store.secure_erase(self.db, self.sid)
        r = self.store.persist_output(self.db, devbox_id=self.dbxA, frame=frame)
        self.assertEqual(r.outcome, DUPLICATE)
        self.assertTrue(r.committed)
        self.assertEqual(self._count(), 1)

    def test_erased_payload_excluded_from_reads(self):
        self._seed(2)
        self.store.secure_erase(self.db, self.sid)
        self.assertEqual(RecordingStore.durable_events(self.db, self.sid), [])
        self.assertEqual(self.store.read_range(self.db, self.sid), [])
        meta = self.store.metadata(self.db, self.sid)
        self.assertEqual(meta["frame_count"], 2)
        self.assertEqual(meta["redacted_count"], 2)
        self.assertEqual(meta["checkpoint_frame_ids"], [])

    def test_empty_session_is_noop(self):
        res = self.store.secure_erase(self.db, self.sid)
        self.assertEqual(res.frame_count, 0)
        self.assertEqual(res.newly_redacted, 0)
        self.assertEqual(res.already_redacted, 0)
        self.assertEqual(res.checkpoints_deleted, 0)

    def test_only_target_session_affected(self):
        # A second session on the same devbox must be left untouched.
        other = "s-" + uuid.uuid4().hex
        self.db.add(models.Session(id=other, user_id=self.uid,
                                   agent_id=self.agA, title="Other"))
        self.db.commit()
        self._seed(2)
        self.store.persist_output(
            self.db, devbox_id=self.dbxA,
            frame=_frame(other, self.pty, 1, "keepme"))
        self.store.secure_erase(self.db, self.sid)
        kept = self.db.query(models.RecordingFrame).filter_by(
            session_id=other).all()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].data, "keepme")
        self.assertIsNone(kept[0].redacted_at)


class OutputAckResponseTests(unittest.TestCase):
    def _r(self, outcome, **kw):
        return output_ack_response(
            PersistResult(outcome=outcome, **kw),
            session_id="s", pty_instance_id="p", seq=42)

    def test_new_and_duplicate_ack(self):
        for oc in (NEW, DUPLICATE):
            r = self._r(oc)
            self.assertEqual(r["type"], "ack")
            self.assertEqual(r["seq"], 42)
            self.assertEqual(r["pty_instance_id"], "p")

    def test_gap_resend_carries_expected(self):
        r = self._r(GAP, expected_seq=7)
        self.assertEqual(r["type"], "resend")
        self.assertEqual(r["expected_seq"], 7)

    def test_conflict_becomes_recoverable_fence(self):
        r = self._r(CONFLICT, reason="payload mismatch")
        self.assertEqual(r["type"], "fence")
        self.assertEqual(r["session_id"], "s")
        self.assertEqual(r["pty_instance_id"], "p")
        self.assertEqual(r["seq"], 42)

    def test_invalid_below_frontier_is_fence(self):
        r = self._r(INVALID, reason="seq 5 below persisted frontier 40")
        self.assertEqual(r["type"], "fence")

    def test_unknown_deleted_session_is_fence(self):
        r = self._r(INVALID, reason="unknown session")
        self.assertEqual(r, {
            "type": "fence", "session_id": "s", "pty_instance_id": "p",
            "seq": 42, "message": "session deleted; abandon this pty_instance",
        })

    def test_invalid_other_stays_terminal_error(self):
        r = self._r(INVALID, reason="not owned by this devbox")
        self.assertEqual(r["type"], "error")
        self.assertEqual(r["message"], "not owned by this devbox")

    def test_invalid_no_reason_defaults(self):
        r = self._r(INVALID)
        self.assertEqual(r["type"], "error")
        self.assertEqual(r["message"], "invalid output frame")


if __name__ == "__main__":
    unittest.main()
