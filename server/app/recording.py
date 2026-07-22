"""RecordingStore — durable Protocol v3 output persistence + ACK gating.

The connector streams PTY output as ordered frames::

    {"type": "output", "session_id": ..., "pty_instance_id": ...,
     "seq": <1-based int>, "data": <str>, "kind": "o",
     "elapsed": <float>, "timestamp": <iso8601>}

Every accepted frame becomes exactly one committed ``models.RecordingFrame`` row. An
ACK is only ever sent *after* the owning row is committed, so the connector can
treat an ACK as a durable-storage guarantee and safely drop its spool.

This module is deliberately pure-ish: it takes an explicit SQLAlchemy session
and an authenticated context and returns a small result object. It performs no
I/O of its own and never touches the live screen or the websocket — the caller
(``main.ws_devbox``) owns those side effects and only performs them for the
outcomes documented below.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import models

# Outcome codes -----------------------------------------------------------
NEW = "new"            # persisted for the first time -> feed live + ACK
DUPLICATE = "duplicate"  # identical payload already committed -> re-ACK only
GAP = "gap"            # seq beyond expected -> ask connector to resend expected
CONFLICT = "conflict"  # same seq, different payload -> protocol violation
INVALID = "invalid"    # malformed / oversized / unknown session / not owned

# Guard rails. asciicast frames are small; a single output chunk far larger
# than this indicates a broken/hostile connector.
MAX_DATA_BYTES = 1 << 20        # 1 MiB of payload data
MAX_FRAME_BYTES = (1 << 20) + 4096

# Auto-checkpoint cadence: snapshot the live screen every N durable NEW frames.
DEFAULT_CHECKPOINT_INTERVAL = 50

# Placeholder written into a RecordingFrame.data when its payload is redacted by
# retention. The seq/hash identity row is preserved so Protocol v3 duplicate-ACK
# semantics keep working; only the human-readable payload is discarded.
REDACTED_PLACEHOLDER = ""


@dataclass
class ErasureResult:
    """Outcome of an owner-invoked :meth:`RecordingStore.secure_erase`.

    ``frame_count`` is the total number of durable frame rows that remain for
    the session (the ledger is *preserved*, never deleted). ``newly_redacted``
    counts frames whose payload this call actually blanked; ``already_redacted``
    counts frames a prior erasure/retention pass had already redacted, so a
    repeated call is idempotent (``newly_redacted == 0`` the second time).
    ``checkpoints_deleted`` is the number of full-screen snapshots removed.
    """
    session_id: str
    frame_count: int
    newly_redacted: int
    already_redacted: int
    checkpoints_deleted: int

    @property
    def redacted_count(self) -> int:
        """Total frames now carrying a redacted (empty) payload."""
        return self.newly_redacted + self.already_redacted


@dataclass
class PersistResult:
    outcome: str
    expected_seq: int | None = None   # populated for GAP
    reason: str | None = None         # populated for INVALID / CONFLICT
    frame: models.RecordingFrame | None = None  # populated for NEW / DUPLICATE

    @property
    def committed(self) -> bool:
        """True when a committed row backs this frame (ACK is permitted)."""
        return self.outcome in (NEW, DUPLICATE)


def output_ack_response(result: "PersistResult", *, session_id, pty_instance_id,
                        seq) -> dict:
    """Map a :class:`PersistResult` to the wire frame the server sends back to
    the connector on the durable-output channel.

    This is the single source of truth for the connector-facing reply and is
    pure (no I/O) so it can be unit-tested exhaustively:

    - NEW / DUPLICATE -> ``ack`` (the durable-commit boundary).
    - GAP             -> ``resend`` at the expected seq.
    - CONFLICT        -> ``fence``: the pty_instance's durable stream has forked
      (connector restarted its PTY but kept re-sending the old spool tail). A
      terminal error here wedges the connector's single-inflight send loop
      forever (reconnect -> resend poison frame -> CONFLICT -> ...), so we emit
      a recoverable fence telling it to abandon this pty_instance's spool tail.
    - INVALID with "unknown session" -> ``fence`` (the session was deleted),
      as does "below persisted frontier" (stale superseded tail); any other
      INVALID stays a terminal ``error`` (genuinely malformed / not owned).
    """
    base = {"session_id": session_id, "pty_instance_id": pty_instance_id}
    if result.outcome in (NEW, DUPLICATE):
        return {"type": "ack", **base, "seq": seq}
    if result.outcome == GAP:
        return {"type": "resend", **base, "expected_seq": result.expected_seq}
    if result.outcome == CONFLICT:
        return {"type": "fence", **base, "seq": seq,
                "message": "pty_instance stream forked; abandon this spool tail"}
    reason = result.reason or "invalid output frame"
    if reason == "unknown session":
        return {"type": "fence", **base, "seq": seq,
                "message": "session deleted; abandon this pty_instance"}
    if "below persisted frontier" in reason:
        return {"type": "fence", **base, "seq": seq,
                "message": "stale spool tail below frontier; "
                           "abandon this pty_instance"}
    return {"type": "error", "session_id": session_id, "message": reason}


def _payload_hash(kind: str, data: str) -> str:
    h = hashlib.sha256()
    h.update((kind or "o").encode("utf-8"))
    h.update(b"\x00")
    h.update(data.encode("utf-8", "replace"))
    return h.hexdigest()


def _parse_timestamp(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _highest_contiguous(db, session_id: str, pty_instance_id: str) -> int:
    """Return the highest seq S such that 1..S are all present (0 if none)."""
    seqs = db.scalars(
        select(models.RecordingFrame.seq)
        .where(
            models.RecordingFrame.session_id == session_id,
            models.RecordingFrame.pty_instance_id == pty_instance_id,
        )
        .order_by(models.RecordingFrame.seq)
    ).all()
    expected = 0
    for s in seqs:
        if s == expected + 1:
            expected = s
        elif s <= expected:
            continue
        else:
            break
    return expected


class RecordingStore:
    """Validation + transactional persistence for v3 output frames."""

    def __init__(self, *, max_data_bytes: int = MAX_DATA_BYTES,
                 max_frame_bytes: int = MAX_FRAME_BYTES,
                 checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
                 commit_hook=None):
        self.max_data_bytes = max_data_bytes
        self.max_frame_bytes = max_frame_bytes
        self.checkpoint_interval = checkpoint_interval
        # Test seam: invoked immediately after commit(), before the caller is
        # told the row is durable. Used to prove commit precedes ACK.
        self.commit_hook = commit_hook

    # -- validation ------------------------------------------------------
    def _validate_ownership(self, db, *, devbox_id: str, session_id: str,
                            frame: dict) -> tuple[object, str | None]:
        sid = frame.get("session_id")
        pty = frame.get("pty_instance_id")
        seq = frame.get("seq")
        data = frame.get("data", "")

        if not sid or not isinstance(sid, str):
            return None, "empty session_id"
        if sid != session_id and session_id is not None:
            # caller may pass session_id=None to trust the frame
            pass
        if not pty or not isinstance(pty, str):
            return None, "empty pty_instance_id"
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return None, "seq must be a positive integer"
        if not isinstance(data, str):
            return None, "data must be a string"
        if len(data.encode("utf-8", "replace")) > self.max_data_bytes:
            return None, "data too large"

        sess = db.get(models.Session, sid)
        if sess is None:
            return None, "unknown session"
        agent = db.get(models.Agent, sess.agent_id)
        if agent is None:
            return None, "session has no agent"
        if agent.id != sess.agent_id:
            return None, "session/agent mismatch"
        if agent.devbox_id != devbox_id:
            return None, "session not owned by authenticated devbox"
        return sess, None

    # -- persistence -----------------------------------------------------
    def persist_output(self, db, *, devbox_id: str, frame: dict,
                       session_id: str | None = None) -> PersistResult:
        """Validate and durably persist one output frame in one step.

        Equivalent to :meth:`classify_output` immediately followed by
        :meth:`commit_new` for a NEW outcome. Kept for callers (replay/append,
        tests) that do not need to interleave live fan-out with the commit.
        The interactive server hot path uses the two-phase API instead so it
        can broadcast to the browser *before* paying the durable-commit cost.
        """
        result = self.classify_output(
            db, devbox_id=devbox_id, frame=frame, session_id=session_id)
        if result.outcome == NEW:
            return self.commit_new(db, result.frame)
        return result

    def classify_output(self, db, *, devbox_id: str, frame: dict,
                        session_id: str | None = None) -> PersistResult:
        """Validate and classify one output frame *without touching disk*.

        Returns a :class:`PersistResult`. For a NEW outcome ``frame`` holds an
        **uncommitted** :class:`models.RecordingFrame` (not yet added to the
        session) that the caller must hand back to :meth:`commit_new` to make
        durable. This lets the hot path feed the live screen and fan out to
        the browser before the (network-disk) commit, so keystroke echo is not
        gated on an fsync. DUPLICATE/GAP/CONFLICT/INVALID need no commit.
        """
        sess, reason = self._validate_ownership(
            db, devbox_id=devbox_id,
            session_id=session_id, frame=frame)
        if reason is not None:
            return PersistResult(INVALID, reason=reason)
        if session_id is not None and frame.get("session_id") != session_id:
            return PersistResult(INVALID, reason="session_id mismatch")

        sid = frame["session_id"]
        pty = frame["pty_instance_id"]
        seq = int(frame["seq"])
        data = frame.get("data", "")
        kind = frame.get("kind", "o") or "o"
        phash = _payload_hash(kind, data)

        # "none" stores only the hash/sequence ledger required to safely
        # re-ACK an identical duplicate; plaintext never reaches a commit.
        return self._classify_locked(
            db, sid, pty, seq, data, kind, phash, frame,
            redact_payload=sess.retention == models.RETENTION_NONE)

    def _classify_locked(self, db, sid, pty, seq, data, kind, phash, frame,
                         redact_payload=False) -> PersistResult:
        # Roll back any prior aborted state so our reads are consistent.
        db.rollback()

        existing = db.scalar(
            select(models.RecordingFrame).where(
                models.RecordingFrame.session_id == sid,
                models.RecordingFrame.pty_instance_id == pty,
                models.RecordingFrame.seq == seq,
            )
        )
        if existing is not None:
            if existing.payload_hash == phash:
                return PersistResult(DUPLICATE, frame=existing)
            return PersistResult(
                CONFLICT, reason="seq already stored with different payload")

        expected = _highest_contiguous(db, sid, pty) + 1
        if seq > expected:
            return PersistResult(GAP, expected_seq=expected)
        if seq < expected:
            # Below the contiguous frontier but not present: unknown / bad.
            return PersistResult(
                INVALID, reason="seq below persisted frontier and not found")

        # Construct but do NOT add/commit; commit_new() owns durability.
        row = models.RecordingFrame(
            session_id=sid,
            pty_instance_id=pty,
            seq=seq,
            kind=kind,
            data=REDACTED_PLACEHOLDER if redact_payload else data,
            payload_hash=phash,
            redacted_at=models.now() if redact_payload else None,
            elapsed=frame.get("elapsed"),
            timestamp=_parse_timestamp(frame.get("timestamp")),
        )
        # Stash the contiguous frontier so a lost commit race can report GAP
        # without re-reading the ledger.
        row._expected_seq = expected  # type: ignore[attr-defined]
        return PersistResult(NEW, frame=row)

    def commit_new(self, db, row, _retried=False) -> PersistResult:
        """Durably commit a NEW row produced by :meth:`classify_output`.

        This is the ACK boundary: the caller must only ACK the frame after
        this returns NEW. On a lost unique-key race the row is reclassified as
        DUPLICATE / CONFLICT / GAP so the caller can recover.
        """
        expected = getattr(row, "_expected_seq", None)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent connection won the unique (session,pty,seq) race.
            db.rollback()
            other = db.scalar(
                select(models.RecordingFrame).where(
                    models.RecordingFrame.session_id == row.session_id,
                    models.RecordingFrame.pty_instance_id == row.pty_instance_id,
                    models.RecordingFrame.seq == row.seq,
                )
            )
            if other is not None and other.payload_hash == row.payload_hash:
                return PersistResult(DUPLICATE, frame=other)
            if other is not None:
                return PersistResult(
                    CONFLICT, reason="lost race with conflicting payload")
            return PersistResult(GAP, expected_seq=expected)

        # Committed: the row is now durable. Fire the test seam *before* the
        # caller learns of the outcome, proving commit precedes ACK.
        if self.commit_hook is not None:
            self.commit_hook(row)
        return PersistResult(NEW, frame=row)

    # -- read path -------------------------------------------------------
    @staticmethod
    def durable_events(db, session_id: str) -> list[tuple[float, str, str]]:
        """Return committed frames as ordered asciicast events.

        Deterministic order: by (pty_instance_id, seq). Elapsed time falls back
        to a monotonically increasing synthetic clock when the connector did
        not supply one, so the merged cast stays valid asciicast v2.
        """
        rows = db.scalars(
            select(models.RecordingFrame)
            .where(
                models.RecordingFrame.session_id == session_id,
                models.RecordingFrame.redacted_at.is_(None),
            )
            .order_by(models.RecordingFrame.pty_instance_id, models.RecordingFrame.seq)
        ).all()
        events: list[tuple[float, str, str]] = []
        last = 0.0
        for r in rows:
            t = r.elapsed if r.elapsed is not None else last
            if t < last:
                t = last
            last = t
            events.append((round(float(t), 6), r.kind or "o", r.data))
        return events

    # -- append (durable output; alias for the persist path) -------------
    def append(self, db, *, devbox_id: str, frame: dict,
               session_id: str | None = None) -> PersistResult:
        """Semantic alias for :meth:`persist_output` used by the replay code."""
        return self.persist_output(
            db, devbox_id=devbox_id, frame=frame, session_id=session_id)

    # -- read a bounded range of durable frames --------------------------
    @staticmethod
    def read_range(db, session_id: str, *, after_frame_id: int | None = None,
                   limit: int | None = None,
                   include_redacted: bool = False) -> list[models.RecordingFrame]:
        """Return committed frames ordered by durable cursor (RecordingFrame.id).

        ``after_frame_id`` selects only frames with id greater than the given
        cursor (used to replay forward from a checkpoint). Redacted frames are
        excluded unless ``include_redacted`` (redacted payload must never leak).
        """
        stmt = (
            select(models.RecordingFrame)
            .where(models.RecordingFrame.session_id == session_id)
            .order_by(models.RecordingFrame.id)
        )
        if after_frame_id is not None:
            stmt = stmt.where(models.RecordingFrame.id > after_frame_id)
        if not include_redacted:
            stmt = stmt.where(models.RecordingFrame.redacted_at.is_(None))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(db.scalars(stmt).all())

    # -- checkpoints -----------------------------------------------------
    def checkpoint(self, db, session_id: str, *, frame_id: int,
                   screen: str, event_index: int = 0,
                   elapsed: float | None = None,
                   cols: int = 80, rows: int = 24) -> models.RecordingCheckpoint:
        """Persist (or update) a full-screen snapshot at a durable frame cursor.

        ``frame_id`` is the ``RecordingFrame.id`` of the last frame folded into
        ``screen`` — an unambiguous cursor even when multiple pty streams
        interleave. Idempotent per (session_id, frame_id).
        """
        existing = db.scalar(
            select(models.RecordingCheckpoint).where(
                models.RecordingCheckpoint.session_id == session_id,
                models.RecordingCheckpoint.frame_id == frame_id,
            )
        )
        if existing is not None:
            existing.screen = screen
            existing.event_index = event_index
            existing.elapsed = elapsed
            existing.cols = cols
            existing.rows = rows
            db.commit()
            return existing
        row = models.RecordingCheckpoint(
            session_id=session_id,
            frame_id=frame_id,
            event_index=event_index,
            elapsed=elapsed,
            cols=cols,
            rows=rows,
            screen=screen,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return db.scalar(
                select(models.RecordingCheckpoint).where(
                    models.RecordingCheckpoint.session_id == session_id,
                    models.RecordingCheckpoint.frame_id == frame_id,
                )
            )
        return row

    def maybe_checkpoint(self, db, session_id: str, *, frame, screen_fn,
                         cols: int = 80, rows: int = 24):
        """Auto-checkpoint after a durable NEW frame when the cadence is hit.

        ``frame`` is the just-committed RecordingFrame. ``screen_fn`` is a
        zero-arg callable returning the serialized live screen (deferred so we
        only render when actually snapshotting). Returns the checkpoint or None.
        """
        if (self.checkpoint_interval <= 0 or frame is None or
                frame.redacted_at is not None):
            return None
        # Count committed, non-redacted frames up to and including this one to
        # derive a stable event ordinal and cadence.
        count = db.scalar(
            select(func.count()).select_from(models.RecordingFrame).where(
                models.RecordingFrame.session_id == session_id,
                models.RecordingFrame.id <= frame.id,
            )
        ) or 0
        if count % self.checkpoint_interval != 0:
            return None
        return self.checkpoint(
            db, session_id, frame_id=frame.id, screen=screen_fn(),
            event_index=count, elapsed=frame.elapsed, cols=cols, rows=rows)

    @staticmethod
    def latest_checkpoint(db, session_id: str,
                          *, at_frame_id: int | None = None):
        """Newest checkpoint at or before a frame cursor (None => latest)."""
        stmt = (
            select(models.RecordingCheckpoint)
            .where(models.RecordingCheckpoint.session_id == session_id)
            .order_by(models.RecordingCheckpoint.frame_id.desc())
        )
        if at_frame_id is not None:
            stmt = stmt.where(models.RecordingCheckpoint.frame_id <= at_frame_id)
        return db.scalar(stmt)

    @staticmethod
    def checkpoints(db, session_id: str) -> list[models.RecordingCheckpoint]:
        return list(db.scalars(
            select(models.RecordingCheckpoint)
            .where(models.RecordingCheckpoint.session_id == session_id)
            .order_by(models.RecordingCheckpoint.frame_id)
        ).all())

    # -- delete ----------------------------------------------------------
    @staticmethod
    def delete(db, session_id: str) -> int:
        """Hard-delete all durable frames and checkpoints for a session.

        Returns the number of frame rows removed. Use for full teardown; for
        retention prefer :meth:`redact_expired` which preserves the ledger.
        """
        db.query(models.RecordingCheckpoint).filter(
            models.RecordingCheckpoint.session_id == session_id).delete()
        n = db.query(models.RecordingFrame).filter(
            models.RecordingFrame.session_id == session_id).delete()
        db.commit()
        return n

    # -- secure erasure --------------------------------------------------
    @staticmethod
    def secure_erase(db, session_id: str, *,
                     now: dt.datetime | None = None) -> ErasureResult:
        """Owner-callable secure erasure of a session's durable recording.

        Redacts the payload of *every* durable frame for ``session_id`` while
        preserving the ledger identity each frame needs for Protocol v3
        duplicate-ACK semantics: the frame row, its ``seq``, ``pty_instance_id``
        and ``payload_hash`` all survive. Only the human-readable ``data`` is
        blanked and ``redacted_at`` is stamped (once — an already-redacted frame
        keeps its original timestamp). All checkpoints are deleted outright,
        because a snapshot may embed now-erased plaintext.

        The whole operation runs in a single transaction, so it is atomic: a
        partial erasure never commits. It is also idempotent — calling it again
        redacts nothing new (``newly_redacted == 0``) and simply reports the
        already-erased ledger. Returns an :class:`ErasureResult` with counts.
        """
        now = now or models.now()
        # Roll back any prior aborted state so our reads and the erasure share
        # one clean, consistent transaction.
        db.rollback()

        frames = db.scalars(
            select(models.RecordingFrame)
            .where(models.RecordingFrame.session_id == session_id)
        ).all()
        newly_redacted = 0
        already_redacted = 0
        for f in frames:
            if f.redacted_at is not None:
                already_redacted += 1
                continue
            # Preserve seq / pty_instance_id / payload_hash: only the payload
            # is discarded and the redaction stamped.
            f.data = REDACTED_PLACEHOLDER
            f.redacted_at = now
            newly_redacted += 1

        checkpoints_deleted = db.query(models.RecordingCheckpoint).filter(
            models.RecordingCheckpoint.session_id == session_id).delete()

        db.commit()
        return ErasureResult(
            session_id=session_id,
            frame_count=len(frames),
            newly_redacted=newly_redacted,
            already_redacted=already_redacted,
            checkpoints_deleted=checkpoints_deleted,
        )

    @staticmethod
    def metadata(db, session_id: str) -> dict:
        """Summarize durable storage for a session (counts, cursors, times)."""
        rows = db.scalars(
            select(models.RecordingFrame)
            .where(models.RecordingFrame.session_id == session_id)
            .order_by(models.RecordingFrame.id)
        ).all()
        total = len(rows)
        redacted = sum(1 for r in rows if r.redacted_at is not None)
        first_id = rows[0].id if rows else None
        last_id = rows[-1].id if rows else None
        ptys = sorted({r.pty_instance_id for r in rows})
        cps = db.scalars(
            select(models.RecordingCheckpoint.frame_id)
            .where(models.RecordingCheckpoint.session_id == session_id)
            .order_by(models.RecordingCheckpoint.frame_id)
        ).all()
        created_first = rows[0].created_at if rows else None
        created_last = rows[-1].created_at if rows else None
        return {
            "session_id": session_id,
            "frame_count": total,
            "redacted_count": redacted,
            "first_frame_id": first_id,
            "last_frame_id": last_id,
            "pty_instance_ids": ptys,
            "checkpoint_frame_ids": list(cps),
            "first_created_at": created_first,
            "last_created_at": created_last,
        }

    # -- retention -------------------------------------------------------
    @classmethod
    def set_retention(cls, db, session: models.Session, retention: str,
                      *, now: dt.datetime | None = None) -> int:
        """Persist a validated session policy and enforce it immediately."""
        if retention not in models.VALID_RETENTIONS:
            raise ValueError("invalid retention policy")
        session.retention = retention
        db.flush()
        return cls.redact_expired(db, now=now)

    @staticmethod
    def redact_expired(db, *, now: dt.datetime | None = None) -> int:
        """Enforce per-session retention by redacting expired frame payloads.

        Preserves Protocol v3 duplicate-ACK semantics: the seq/payload_hash
        identity row is kept, only ``data`` is blanked and ``redacted_at`` set.
        ``none`` retention redacts immediately; ``permanent`` never expires.
        Returns the number of frames newly redacted.
        """
        now = now or models.now()
        sessions = db.scalars(select(models.Session)).all()
        redacted = 0
        for sess in sessions:
            policy = getattr(sess, "retention", None) or models.RETENTION_30D
            days = models.RETENTION_DAYS.get(policy, 30)
            if days is None:
                continue  # permanent
            cutoff = now - dt.timedelta(days=days) if days > 0 else now
            frames = db.scalars(
                select(models.RecordingFrame).where(
                    models.RecordingFrame.session_id == sess.id,
                    models.RecordingFrame.redacted_at.is_(None),
                )
            ).all()
            sess_redacted = 0
            for f in frames:
                # For "none" (days == 0) redact everything; otherwise redact
                # frames older than the cutoff. Normalize tz-awareness since
                # SQLite returns naive datetimes.
                ref = f.created_at or now
                ref_cmp = ref.replace(tzinfo=None) if ref.tzinfo else ref
                cutoff_cmp = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
                if days == 0 or ref_cmp <= cutoff_cmp:
                    f.data = REDACTED_PLACEHOLDER
                    f.redacted_at = now
                    sess_redacted += 1
            redacted += sess_redacted
            # Drop checkpoints that captured now-redacted content.
            if sess_redacted:
                db.query(models.RecordingCheckpoint).filter(
                    models.RecordingCheckpoint.session_id == sess.id).delete()
        db.commit()
        return redacted
