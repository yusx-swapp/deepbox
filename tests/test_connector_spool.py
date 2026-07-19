"""Protocol v3 durable SQLite spool tests.

Covers: per-instance monotonic seq, interleaved deterministic order, durable
offline backlog across reopen, ACK lost/reopen, stale/future/gap rejection,
pending bytes accounting, input dedup across restart, namespace isolation and
canonicalization, POSIX permissions, exclusive ownership, corrupt-DB
fail-closed, and failed-transaction-doesn't-advance-seq. Windows compatible.
"""
import json
import os
import tempfile
import unittest

from connector.spool import (
    DiskSpool,
    InMemorySpool,
    SpoolCorruptionError,
    SpoolInUseError,
    SpoolRecord,
    canonicalize_url,
    open_spool,
    spool_namespace,
    spool_path,
    IS_WIN,
)


def _frame(sid, pid, **extra):
    f = {"type": "output", "session_id": sid, "pty_instance_id": pid}
    f.update(extra)
    return f


class DiskSpoolSeqTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_per_instance_seq_is_independent_and_positive(self):
        s = DiskSpool(self.path)
        self.assertEqual(s.enqueue_output(_frame("A", "p1")), 1)
        self.assertEqual(s.enqueue_output(_frame("A", "p1")), 2)
        # Different instance restarts its own sequence at 1.
        self.assertEqual(s.enqueue_output(_frame("A", "p2")), 1)
        self.assertEqual(s.enqueue_output(_frame("B", "p1")), 1)
        self.assertEqual(s.enqueue_output(_frame("A", "p1")), 3)
        s.close()

    def test_seq_injected_into_payload(self):
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1", data="hi"))
        rec = s.records()[0]
        self.assertIsInstance(rec, SpoolRecord)
        self.assertEqual(rec.seq, 1)
        self.assertEqual(rec.frame["seq"], 1)
        self.assertEqual(rec.frame["data"], "hi")
        # payload_bytes matches the canonical serialization length.
        self.assertEqual(rec.payload_bytes,
                         len(json.dumps(rec.frame, sort_keys=True,
                                        separators=(",", ":")).encode()))
        s.close()

    def test_interleaved_emission_preserves_global_order(self):
        s = DiskSpool(self.path)
        order = [("A", "p1"), ("B", "p1"), ("A", "p1"), ("A", "p2"), ("B", "p1")]
        for i, (sid, pid) in enumerate(order):
            s.enqueue_output(_frame(sid, pid, n=i))
        recs = s.records()
        self.assertEqual([r.frame["n"] for r in recs], [0, 1, 2, 3, 4])
        self.assertEqual([(r.session_id, r.pty_instance_id, r.seq) for r in recs],
                         [("A", "p1", 1), ("B", "p1", 1), ("A", "p1", 2),
                          ("A", "p2", 1), ("B", "p1", 2)])
        s.close()

    def test_missing_identity_rejected(self):
        s = DiskSpool(self.path)
        with self.assertRaises(ValueError):
            s.enqueue_output({"type": "output", "session_id": "", "pty_instance_id": "p"})
        with self.assertRaises(ValueError):
            s.enqueue_output({"type": "output", "session_id": "A"})
        s.close()
class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_offline_backlog_survives_reopen(self):
        s = DiskSpool(self.path)
        for i in range(5):
            s.enqueue_output(_frame("A", "p1", n=i))
        s.close()

        s2 = DiskSpool(self.path)
        recs = s2.records()
        self.assertEqual([r.frame["n"] for r in recs], [0, 1, 2, 3, 4])
        self.assertEqual([r.seq for r in recs], [1, 2, 3, 4, 5])
        s2.close()

    def test_no_seq_reuse_after_ack_and_restart(self):
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1"))
        s.enqueue_output(_frame("A", "p1"))
        self.assertTrue(s.ack("A", "p1", 1))
        self.assertTrue(s.ack("A", "p1", 2))
        self.assertEqual(s.records(), [])
        s.close()

        s2 = DiskSpool(self.path)
        # New frame must be seq 3 -- above every seq ever used.
        self.assertEqual(s2.enqueue_output(_frame("A", "p1")), 3)
        self.assertEqual(s2.last_acked("A", "p1"), 2)
        s2.close()

    def test_ack_lost_before_persist_is_redelivered(self):
        # Simulate: transport acked seq 1 but supervisor crashed before ack()
        # was called. On reopen the frame is still pending and re-emitted.
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1", n=0))
        s.enqueue_output(_frame("A", "p1", n=1))
        s.close()
        s2 = DiskSpool(self.path)
        self.assertEqual([r.frame["n"] for r in s2.records()], [0, 1])
        self.assertEqual(s2.last_acked("A", "p1"), 0)
        s2.close()


class AckSemanticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _both(self):
        return [DiskSpool(self.path), InMemorySpool()]

    def test_contiguous_fifo_ack(self):
        for s in self._both():
            s.enqueue_output(_frame("A", "p1"))
            s.enqueue_output(_frame("A", "p1"))
            s.enqueue_output(_frame("A", "p1"))
            self.assertTrue(s.ack("A", "p1", 1))
            self.assertEqual([r.seq for r in s.records()], [2, 3])
            self.assertEqual(s.last_acked("A", "p1"), 1)
            s.close()
            if isinstance(s, DiskSpool):
                os.remove(self.path)

    def test_future_ack_rejected(self):
        for s in self._both():
            s.enqueue_output(_frame("A", "p1"))
            s.enqueue_output(_frame("A", "p1"))
            self.assertFalse(s.ack("A", "p1", 2))  # not the smallest pending
            self.assertEqual([r.seq for r in s.records()], [1, 2])
            self.assertEqual(s.last_acked("A", "p1"), 0)
            s.close()
            if isinstance(s, DiskSpool):
                os.remove(self.path)

    def test_stale_ack_rejected(self):
        for s in self._both():
            s.enqueue_output(_frame("A", "p1"))
            self.assertTrue(s.ack("A", "p1", 1))
            self.assertFalse(s.ack("A", "p1", 1))  # already acked
            s.close()
            if isinstance(s, DiskSpool):
                os.remove(self.path)

    def test_unknown_instance_ack_rejected(self):
        for s in self._both():
            s.enqueue_output(_frame("A", "p1"))
            self.assertFalse(s.ack("Z", "zz", 1))
            self.assertFalse(s.ack("A", "p1", 5))
            self.assertEqual([r.seq for r in s.records()], [1])
            s.close()
            if isinstance(s, DiskSpool):
                os.remove(self.path)

    def test_gap_ack_rejected(self):
        for s in self._both():
            s.enqueue_output(_frame("A", "p1"))
            s.enqueue_output(_frame("A", "p1"))
            self.assertTrue(s.ack("A", "p1", 1))
            # Trying to skip to 3 (a gap; 2 is the only pending) is rejected.
            self.assertFalse(s.ack("A", "p1", 3))
            self.assertTrue(s.ack("A", "p1", 2))
            s.close()
            if isinstance(s, DiskSpool):
                os.remove(self.path)

    def test_ack_persists_across_reopen(self):
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1"))
        s.enqueue_output(_frame("A", "p1"))
        self.assertTrue(s.ack("A", "p1", 1))
        s.close()
        s2 = DiskSpool(self.path)
        self.assertEqual([r.seq for r in s2.records()], [2])
        self.assertEqual(s2.last_acked("A", "p1"), 1)
        s2.close()


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_pending_bytes_and_frames(self):
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1", data="x"))
        s.enqueue_output(_frame("B", "p1", data="yy"))
        st = s.status()
        self.assertEqual(st["pending_frames"], 2)
        self.assertEqual(st["pending_bytes"], sum(r.payload_bytes for r in s.records()))
        self.assertGreater(st["pending_bytes"], 0)
        s.ack("A", "p1", 1)
        self.assertEqual(s.status()["pending_frames"], 1)
        self.assertEqual(s.status()["max_last_ack"], 1)
        s.close()


class InputDedupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dedup_within_session(self):
        s = DiskSpool(self.path)
        self.assertTrue(s.record_input_once("abc"))
        self.assertFalse(s.record_input_once("abc"))
        self.assertTrue(s.record_input_once("def"))
        s.close()

    def test_dedup_survives_restart(self):
        s = DiskSpool(self.path)
        self.assertTrue(s.record_input_once("id-1"))
        s.close()
        s2 = DiskSpool(self.path)
        self.assertFalse(s2.record_input_once("id-1"))
        self.assertTrue(s2.record_input_once("id-2"))
        s2.close()

    def test_input_id_validation(self):
        s = DiskSpool(self.path)
        with self.assertRaises(ValueError):
            s.record_input_once("")
        with self.assertRaises(ValueError):
            s.record_input_once("x" * 100000)
        s.close()

    def test_prune_by_max_entries(self):
        s = DiskSpool(self.path)
        for i in range(10):
            s.record_input_once(f"id-{i}")
        removed = s.prune_input_receipts(max_entries=3)
        self.assertEqual(removed, 7)
        s.close()


class NamespaceTests(unittest.TestCase):
    def test_deterministic_and_isolated(self):
        a = spool_namespace("https://s.example", "tokA")
        b = spool_namespace("https://s.example", "tokA")
        c = spool_namespace("https://s.example", "tokB")
        d = spool_namespace("https://other.example", "tokA")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)

    def test_equivalent_urls_canonicalize_same(self):
        self.assertEqual(
            spool_namespace("https://S.Example:443/", "t"),
            spool_namespace("https://s.example", "t"),
        )
        self.assertEqual(
            spool_namespace("http://Host:80/path/", "t"),
            spool_namespace("http://host/path", "t"),
        )

    def test_canonicalize_forms(self):
        self.assertEqual(canonicalize_url("HTTPS://S.Example:443/"), "https://s.example")
        self.assertEqual(canonicalize_url("http://h:80"), "http://h")
        self.assertEqual(canonicalize_url("https://h:8443/a/"), "https://h:8443/a")

    def test_token_never_in_path(self):
        p = spool_path("https://s.example", "super-secret-token", root="/tmp/x")
        self.assertNotIn("super-secret-token", p)

    def test_open_spool_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = open_spool("https://s.example", "tok", root=tmp)
            s.enqueue_output(_frame("A", "p1"))
            s.close()
            s2 = open_spool("https://s.example:443/", "tok", root=tmp)
            self.assertEqual(len(s2.records()), 1)
            s2.close()


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_exclusive_owner(self):
        s = DiskSpool(self.path)
        with self.assertRaises(SpoolInUseError):
            DiskSpool(self.path)
        s.close()
        # After release a new owner can acquire.
        s2 = DiskSpool(self.path)
        s2.close()

    @unittest.skipIf(IS_WIN, "POSIX permission check")
    def test_posix_permissions(self):
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.path)).st_mode & 0o777, 0o700)
        s.close()


class CorruptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_not_a_database_fails_closed(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as fh:
            fh.write(b"this is definitely not a sqlite database file, at all\n" * 10)
        with self.assertRaises(SpoolCorruptionError):
            DiskSpool(self.path)

    def test_truncated_database_fails_closed(self):
        s = DiskSpool(self.path)
        for i in range(20):
            s.enqueue_output(_frame("A", "p1", n=i))
        s.close()
        # Clobber the header region so it's no longer a valid DB.
        with open(self.path, "r+b") as fh:
            fh.seek(0)
            fh.write(b"\x00" * 64)
        with self.assertRaises(SpoolCorruptionError):
            DiskSpool(self.path)


class TransactionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ns", "spool.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_enqueue_does_not_advance_seq(self):
        s = DiskSpool(self.path)
        s.enqueue_output(_frame("A", "p1"))  # seq 1

        # Force the INSERT inside enqueue_output to fail mid-transaction by
        # monkeypatching _dumps to raise, then confirm no seq was consumed.
        import connector.spool as mod
        orig = mod._dumps

        def boom(_obj):
            raise RuntimeError("serialize failed")

        mod._dumps = boom
        try:
            with self.assertRaises(RuntimeError):
                s.enqueue_output(_frame("A", "p1"))
        finally:
            mod._dumps = orig

        # The next successful enqueue must reuse seq 2 (the failed one didn't
        # consume it) and the outbox still has exactly the first frame plus one.
        self.assertEqual(s.enqueue_output(_frame("A", "p1")), 2)
        self.assertEqual([r.seq for r in s.records()], [1, 2])
        s.close()


if __name__ == "__main__":
    unittest.main()
