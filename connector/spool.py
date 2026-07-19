r"""Durable output spool for the connector (Protocol v3).

The supervisor buffers PTY output frames until a transport confirms the server
accepted them. This module makes that buffer durable using the standard library
``sqlite3`` module so a supervisor crash never loses un-acknowledged output.

Design
------

Everything lives in one SQLite database opened in WAL mode with
``synchronous=FULL`` so that a committed row survives power loss. Three tables:

``outbox``
    One row per emitted output frame that has not yet been acknowledged. The
    per-``(session_id, pty_instance_id)`` sequence number (``seq``) is assigned
    inside the same ``IMMEDIATE`` transaction that inserts the row, computed as
    ``max(last_acked_seq, max(existing outbox seq)) + 1`` so it is always
    positive, monotonic, and never reused across ACK or restart. An
    autoincrementing ``ord`` column records global insertion order so pending
    frames replay in exactly the order they were emitted, even across many
    interleaved sessions.

``ack_state``
    The high-water ``last_acked_seq`` per ``(session_id, pty_instance_id)``.
    ACK is strict, contiguous FIFO: only the current smallest pending seq for
    an instance -- which must equal ``last_acked_seq + 1`` -- can advance. Any
    stale, future, or unknown seq is rejected without mutating anything.

``input_receipts``
    Deduplication ledger for inbound ``client_input_id`` values so a given
    input is applied at most once, even across a restart.

Wire/record model: a frame is serialized as canonical compact JSON (sorted
keys, no whitespace) with its assigned ``seq`` injected under the ``"seq"`` key
before serialization, so the persisted payload is exactly what is emitted.

Isolation & secrecy: :func:`spool_namespace` derives a deterministic, opaque
directory/file identity from the canonicalized server URL plus a SHA-256 hash
of the token. The raw token is never written. Different URLs or tokens map to
different databases; equivalent URLs canonicalize to the same one.

Ownership: exactly one live process may own a spool. A sibling lock file is
acquired non-blocking (``fcntl`` on POSIX, ``msvcrt`` on Windows); a second
opener raises :class:`SpoolInUseError`.

Corruption is fail-closed: a file that is not a valid SQLite database raises
:class:`SpoolCorruptionError` rather than being silently reset.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

IS_WIN = sys.platform.startswith("win")

# Bound on client_input_id to keep the dedup ledger sane and reject abuse.
MAX_INPUT_ID_LEN = 256


class SpoolError(Exception):
    """Base class for spool errors."""


class SpoolCorruptionError(SpoolError):
    """The spool database is corrupt or not a SQLite database (fail-closed)."""


class SpoolInUseError(SpoolError):
    """Another live owner already holds this spool's lock."""


@dataclass(frozen=True)
class SpoolRecord:
    """A decoded pending outbox record."""

    session_id: str
    pty_instance_id: str
    seq: int
    frame: dict
    created_at: float
    payload_bytes: int


def _dumps(obj: dict) -> str:
    """Serialize to canonical compact JSON (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def canonicalize_url(server_url: str) -> str:
    """Return a canonical form of ``server_url`` for stable namespacing.

    Lowercases scheme and host, strips a default port for the scheme, and
    normalizes the path (trailing slashes collapsed to a bare root).
    """
    if not server_url or not str(server_url).strip():
        raise ValueError("server_url must be nonempty")
    parts = urlsplit(str(server_url).strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    # Normalize path: strip trailing slashes; empty path stays empty.
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def spool_namespace(server_url: str, token: str) -> str:
    """Deterministic opaque identity from canonical URL + SHA-256(token).

    The raw token is never included in the result.
    """
    canon = canonicalize_url(server_url)
    token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    digest = hashlib.sha256(f"{canon}\x00{token_hash}".encode("utf-8")).hexdigest()
    return digest[:32]


def default_spool_root() -> str:
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "deepbox", "spool")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "deepbox", "spool")


def spool_path(server_url: str, token: str, root: str | None = None) -> str:
    """Filesystem path for the spool DB identified by (server_url, token)."""
    ns = spool_namespace(server_url, token)
    root = root or default_spool_root()
    return os.path.join(root, ns, "spool.db")


# ---------------------------------------------------------------------------
# Permissions & locking
# ---------------------------------------------------------------------------

def _chmod_best_effort(path: str, mode: int) -> None:
    if IS_WIN:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class _OwnerLock:
    """Non-blocking exclusive lock on a sibling ``.lock`` file."""

    def __init__(self, lock_path: str):
        self._path = lock_path
        self._fh = None

    def acquire(self) -> None:
        fh = open(self._path, "a+b")
        try:
            if IS_WIN:
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise SpoolInUseError(
                f"spool already owned by another process: {self._path}"
            )
        _chmod_best_effort(self._path, 0o600)
        self._fh = fh

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if IS_WIN:
                import msvcrt

                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            finally:
                self._fh = None


# ---------------------------------------------------------------------------
# Spool base / in-memory
# ---------------------------------------------------------------------------

class SpoolBase:
    """Interface shared by :class:`DiskSpool` and :class:`InMemorySpool`."""

    # -- Protocol v3 primary API ------------------------------------------
    def enqueue_output(self, frame: dict) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def pending_records(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def ack(self, session_id: str, pty_instance_id: str, seq: int) -> bool:  # pragma: no cover
        raise NotImplementedError

    def last_acked(self, session_id: str, pty_instance_id: str) -> int:  # pragma: no cover
        raise NotImplementedError

    def status(self) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def record_input_once(self, client_input_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- Backward-compatible supervisor seam ------------------------------
    # The supervisor treats the spool as a single ordered FIFO keyed by the
    # global insertion order and identifies records by the global ``ord``. The
    # helpers below adapt the per-instance v3 API onto that older surface so the
    # existing supervisor keeps working unchanged.
    def append_frame(self, frame: dict) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def oldest_seq(self):  # pragma: no cover - abstract
        raise NotImplementedError


def _identity(frame: dict):
    sid = frame.get("_spool_session_id", frame.get("session_id")) or ""
    pid = frame.get("_spool_pty_instance_id", frame.get("pty_instance_id")) or ""
    return str(sid), str(pid)


def _legacy_frame(frame: dict) -> dict:
    """Inject a fallback pty_instance_id for legacy supervisor frames.

    The older supervisor emits ``agent_id``/``session_id`` frames without a
    ``pty_instance_id``. ``append_frame`` routes through here so those frames
    get a stable per-instance key derived from ``agent_id``.
    """
    out = dict(frame)
    if frame.get("type") != "output":
        # Controls share a private spool stream so their local delivery sequence
        # never creates holes in a PTY output stream. Private keys are stripped
        # before the frame is serialized or sent.
        out["_spool_session_id"] = frame.get("session_id") or frame.get("agent_id") or "-"
        out["_spool_pty_instance_id"] = "control"
    elif not out.get("session_id") or not out.get("pty_instance_id"):
        raise ValueError("output frame requires session_id and pty_instance_id")
    return out


class InMemorySpool(SpoolBase):
    """Volatile spool with the same semantics as :class:`DiskSpool`.

    Used by unit tests to exercise supervisor logic without touching disk.
    """

    def __init__(self):
        self._rows: list[dict] = []  # each: sid,pid,seq,frame,created,ord,bytes
        self._last_acked: dict[tuple[str, str], int] = {}
        self._ord = 0
        self._inputs: dict[str, float] = {}

    def _next_seq(self, key) -> int:
        base = self._last_acked.get(key, 0)
        for r in self._rows:
            if (r["sid"], r["pid"]) == key and r["seq"] > base:
                base = r["seq"]
        return base + 1

    def enqueue_output(self, frame: dict) -> int:
        sid, pid = _identity(frame)
        if not sid or not pid:
            raise ValueError("frame requires nonempty session_id and pty_instance_id")
        key = (sid, pid)
        seq = self._next_seq(key)
        emitted = dict(frame)
        emitted.pop("_spool_session_id", None)
        emitted.pop("_spool_pty_instance_id", None)
        emitted["seq"] = seq
        payload = _dumps(emitted)
        self._ord += 1
        self._rows.append({
            "sid": sid, "pid": pid, "seq": seq, "frame": emitted,
            "created": time.time(), "ord": self._ord,
            "bytes": len(payload.encode("utf-8")),
        })
        return seq

    def pending_records(self):
        rows = sorted(self._rows, key=lambda r: r["ord"])
        return [(r["ord"], r["frame"]) for r in rows]

    def records(self) -> list[SpoolRecord]:
        rows = sorted(self._rows, key=lambda r: r["ord"])
        return [SpoolRecord(r["sid"], r["pid"], r["seq"], r["frame"],
                            r["created"], r["bytes"]) for r in rows]

    def ack(self, *args) -> bool:
        sid, pid, seq = _ack_args(self, args)
        if sid is None:
            return False
        key = (sid, pid)
        cand = [r for r in self._rows if (r["sid"], r["pid"]) == key]
        if not cand:
            return False
        smallest = min(cand, key=lambda r: r["seq"])
        last = self._last_acked.get(key, 0)
        if seq != smallest["seq"] or seq != last + 1:
            return False
        self._rows.remove(smallest)
        self._last_acked[key] = seq
        return True

    def last_acked(self, session_id: str, pty_instance_id: str) -> int:
        return self._last_acked.get((str(session_id), str(pty_instance_id)), 0)

    def status(self) -> dict:
        pending_bytes = sum(r["bytes"] for r in self._rows)
        last_ack = {f"{k[0]}/{k[1]}": v for k, v in self._last_acked.items()}
        return {
            "pending_frames": len(self._rows),
            "pending_bytes": pending_bytes,
            "last_ack": last_ack,
            "max_last_ack": max(self._last_acked.values(), default=0),
        }

    def record_input_once(self, client_input_id: str) -> bool:
        cid = _validate_input_id(client_input_id)
        if cid in self._inputs:
            return False
        self._inputs[cid] = time.time()
        return True

    def prune_input_receipts(self, max_age: float | None = None,
                             max_entries: int | None = None) -> int:
        removed = 0
        if max_age is not None:
            cutoff = time.time() - max_age
            for cid in [c for c, t in self._inputs.items() if t < cutoff]:
                del self._inputs[cid]
                removed += 1
        if max_entries is not None and len(self._inputs) > max_entries:
            ordered = sorted(self._inputs.items(), key=lambda kv: kv[1])
            for cid, _ in ordered[: len(self._inputs) - max_entries]:
                del self._inputs[cid]
                removed += 1
        return removed

    def close(self) -> None:
        return None

    # -- backward-compatible supervisor seam ------------------------------
    def append_frame(self, frame: dict) -> int:
        self.enqueue_output(_legacy_frame(frame))
        return self._ord

    def oldest_seq(self):
        if not self._rows:
            return None
        return min(self._rows, key=lambda r: r["ord"])["ord"]


def _validate_input_id(client_input_id) -> str:
    if not isinstance(client_input_id, str) or not client_input_id:
        raise ValueError("client_input_id must be a nonempty string")
    if len(client_input_id) > MAX_INPUT_ID_LEN:
        raise ValueError("client_input_id too long")
    return client_input_id


def _ack_args(spool, args):
    """Normalize ack() arguments supporting both v3 and legacy call forms.

    v3:     ack(session_id, pty_instance_id, seq)
    legacy: ack(ord)  -> resolve to the identity of that global-order row
    """
    if len(args) == 3:
        sid, pid, seq = args
        return str(sid), str(pid), int(seq)
    if len(args) == 1:
        return spool._resolve_legacy_ack(int(args[0]))
    raise TypeError("ack() takes (session_id, pty_instance_id, seq) or (ord)")


# ---------------------------------------------------------------------------
# Disk spool (SQLite)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    ord INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    pty_instance_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload_bytes INTEGER NOT NULL,
    UNIQUE(session_id, pty_instance_id, seq)
);
CREATE TABLE IF NOT EXISTS ack_state (
    session_id TEXT NOT NULL,
    pty_instance_id TEXT NOT NULL,
    last_acked_seq INTEGER NOT NULL,
    PRIMARY KEY(session_id, pty_instance_id)
);
CREATE TABLE IF NOT EXISTS input_receipts (
    client_input_id TEXT PRIMARY KEY,
    delivered_at REAL NOT NULL
);
"""


class DiskSpool(SpoolBase):
    """Durable SQLite-backed spool."""

    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        _chmod_best_effort(parent, 0o700)

        self._lock = _OwnerLock(path + ".lock")
        self._lock.acquire()
        try:
            self._open_db()
        except Exception:
            self._lock.release()
            raise

    # -- lifecycle --------------------------------------------------------
    def _open_db(self) -> None:
        existed = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if existed:
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    conn.close()
                    raise SpoolCorruptionError(
                        f"spool integrity check failed: {self.path}"
                    )
            except sqlite3.DatabaseError as e:
                conn.close()
                raise SpoolCorruptionError(
                    f"spool is not a valid database: {self.path}: {e}"
                ) from e
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError as e:
            conn.close()
            raise SpoolCorruptionError(
                f"spool is not a valid database: {self.path}: {e}"
            ) from e
        self._conn = conn
        _chmod_best_effort(self.path, 0o600)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        self._lock.release()

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise SpoolError("spool is closed")
        return self._conn

    # -- v3 API -----------------------------------------------------------
    def enqueue_output(self, frame: dict) -> int:
        sid, pid = _identity(frame)
        if not sid or not pid:
            raise ValueError("frame requires nonempty session_id and pty_instance_id")
        conn = self._require()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT MAX(seq) AS m FROM outbox "
                "WHERE session_id=? AND pty_instance_id=?",
                (sid, pid),
            ).fetchone()
            max_out = row["m"] if row and row["m"] is not None else 0
            row = conn.execute(
                "SELECT last_acked_seq AS m FROM ack_state "
                "WHERE session_id=? AND pty_instance_id=?",
                (sid, pid),
            ).fetchone()
            last_ack = row["m"] if row and row["m"] is not None else 0
            seq = max(max_out, last_ack) + 1
            emitted = dict(frame)
            emitted.pop("_spool_session_id", None)
            emitted.pop("_spool_pty_instance_id", None)
            emitted["seq"] = seq
            payload = _dumps(emitted)
            pbytes = len(payload.encode("utf-8"))
            conn.execute(
                "INSERT INTO outbox(session_id, pty_instance_id, seq, payload, "
                "created_at, payload_bytes) VALUES (?,?,?,?,?,?)",
                (sid, pid, seq, payload, time.time(), pbytes),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return seq

    def _rows(self):
        conn = self._require()
        return conn.execute(
            "SELECT ord, session_id, pty_instance_id, seq, payload, "
            "created_at, payload_bytes FROM outbox ORDER BY ord ASC"
        ).fetchall()

    def records(self) -> list[SpoolRecord]:
        out = []
        for r in self._rows():
            out.append(SpoolRecord(
                r["session_id"], r["pty_instance_id"], r["seq"],
                json.loads(r["payload"]), r["created_at"], r["payload_bytes"],
            ))
        return out

    def pending_records(self):
        return [(r["ord"], json.loads(r["payload"])) for r in self._rows()]

    def ack(self, *args) -> bool:
        sid, pid, seq = _ack_args(self, args)
        if sid is None:
            return False
        conn = self._require()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT MIN(seq) AS s FROM outbox "
                "WHERE session_id=? AND pty_instance_id=?",
                (sid, pid),
            ).fetchone()
            smallest = row["s"] if row and row["s"] is not None else None
            if smallest is None:
                conn.execute("ROLLBACK")
                return False
            row = conn.execute(
                "SELECT last_acked_seq AS m FROM ack_state "
                "WHERE session_id=? AND pty_instance_id=?",
                (sid, pid),
            ).fetchone()
            last = row["m"] if row and row["m"] is not None else 0
            if seq != smallest or seq != last + 1:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO ack_state(session_id, pty_instance_id, last_acked_seq) "
                "VALUES (?,?,?) ON CONFLICT(session_id, pty_instance_id) "
                "DO UPDATE SET last_acked_seq=excluded.last_acked_seq",
                (sid, pid, seq),
            )
            conn.execute(
                "DELETE FROM outbox WHERE session_id=? AND pty_instance_id=? AND seq=?",
                (sid, pid, seq),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True

    def last_acked(self, session_id: str, pty_instance_id: str) -> int:
        conn = self._require()
        row = conn.execute(
            "SELECT last_acked_seq AS m FROM ack_state "
            "WHERE session_id=? AND pty_instance_id=?",
            (str(session_id), str(pty_instance_id)),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def status(self) -> dict:
        conn = self._require()
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(payload_bytes),0) AS b FROM outbox"
        ).fetchone()
        acks = conn.execute(
            "SELECT session_id, pty_instance_id, last_acked_seq FROM ack_state"
        ).fetchall()
        last_ack = {f"{a['session_id']}/{a['pty_instance_id']}": a["last_acked_seq"]
                    for a in acks}
        return {
            "pending_frames": int(row["n"]),
            "pending_bytes": int(row["b"]),
            "last_ack": last_ack,
            "max_last_ack": max(last_ack.values(), default=0),
        }

    def record_input_once(self, client_input_id: str) -> bool:
        cid = _validate_input_id(client_input_id)
        conn = self._require()
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                conn.execute(
                    "INSERT INTO input_receipts(client_input_id, delivered_at) "
                    "VALUES (?,?)",
                    (cid, time.time()),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True

    def prune_input_receipts(self, max_age: float | None = None,
                             max_entries: int | None = None) -> int:
        conn = self._require()
        removed = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            if max_age is not None:
                cutoff = time.time() - max_age
                cur = conn.execute(
                    "DELETE FROM input_receipts WHERE delivered_at < ?", (cutoff,)
                )
                removed += cur.rowcount
            if max_entries is not None:
                cur = conn.execute(
                    "DELETE FROM input_receipts WHERE client_input_id IN ("
                    "SELECT client_input_id FROM input_receipts "
                    "ORDER BY delivered_at DESC, client_input_id "
                    "LIMIT -1 OFFSET ?)",
                    (max_entries,),
                )
                removed += cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return removed

    # -- backward-compatible supervisor seam ------------------------------
    def append_frame(self, frame: dict) -> int:
        self.enqueue_output(_legacy_frame(frame))
        conn = self._require()
        row = conn.execute("SELECT MAX(ord) AS m FROM outbox").fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def oldest_seq(self):
        conn = self._require()
        row = conn.execute("SELECT MIN(ord) AS m FROM outbox").fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    def _resolve_legacy_ack(self, ordv: int):
        """Map a global ``ord`` onto the (sid, pid, seq) v3 ack tuple."""
        conn = self._require()
        row = conn.execute(
            "SELECT session_id, pty_instance_id, seq FROM outbox WHERE ord=?",
            (ordv,),
        ).fetchone()
        if row is None:
            return (None, None, None)
        return (row["session_id"], row["pty_instance_id"], int(row["seq"]))


# InMemorySpool legacy ack resolution mirror.
def _im_resolve(self, ordv):
    for r in self._rows:
        if r["ord"] == ordv:
            return (r["sid"], r["pid"], r["seq"])
    return (None, None, None)


InMemorySpool._resolve_legacy_ack = _im_resolve


# ---------------------------------------------------------------------------
# Convenience opener
# ---------------------------------------------------------------------------

def open_spool(server_url: str, token: str, root: str | None = None) -> DiskSpool:
    """Open (or create) the durable spool for (server_url, token)."""
    return DiskSpool(spool_path(server_url, token, root=root))
