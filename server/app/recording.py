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
                 commit_hook=None):
        self.max_data_bytes = max_data_bytes
        self.max_frame_bytes = max_frame_bytes
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
        """Validate and durably persist one output frame.

        ``devbox_id`` is the authenticated connector. ``session_id`` is an
        optional expectation; when provided the frame's session must match.
        Returns a :class:`PersistResult`; the caller must only ACK / feed the
        live screen based on that outcome.
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

        return self._persist_locked(db, sid, pty, seq, data, kind, phash, frame)

    def _persist_locked(self, db, sid, pty, seq, data, kind, phash, frame,
                        _retried=False) -> PersistResult:
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

        row = models.RecordingFrame(
            session_id=sid,
            pty_instance_id=pty,
            seq=seq,
            kind=kind,
            data=data,
            payload_hash=phash,
            elapsed=frame.get("elapsed"),
            timestamp=_parse_timestamp(frame.get("timestamp")),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent connection won the unique (session,pty,seq) race.
            db.rollback()
            if _retried:
                # Requery once more definitively.
                other = db.scalar(
                    select(models.RecordingFrame).where(
                        models.RecordingFrame.session_id == sid,
                        models.RecordingFrame.pty_instance_id == pty,
                        models.RecordingFrame.seq == seq,
                    )
                )
                if other is not None and other.payload_hash == phash:
                    return PersistResult(DUPLICATE, frame=other)
                if other is not None:
                    return PersistResult(
                        CONFLICT, reason="lost race with conflicting payload")
                return PersistResult(GAP, expected_seq=expected)
            return self._persist_locked(
                db, sid, pty, seq, data, kind, phash, frame, _retried=True)

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
            .where(models.RecordingFrame.session_id == session_id)
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
