"""SQLAlchemy models + engine/session for deepbox."""
from __future__ import annotations

import datetime as dt
from sqlalchemy import (
    create_engine, String, Text, ForeignKey, DateTime, JSON, Integer, Float,
    UniqueConstraint, inspect, text, event,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)

PROTOCOL_VERSION = 3

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
VALID_ROLES = {ROLE_OWNER, ROLE_MEMBER}

# Collaboration membership roles (Cut 8), ordered least->most privileged.
WS_ROLE_VIEWER = "viewer"
WS_ROLE_OPERATOR = "operator"
WS_ROLE_ADMIN = "admin"
WS_ROLE_OWNER = "owner"
VALID_WS_ROLES = {WS_ROLE_VIEWER, WS_ROLE_OPERATOR, WS_ROLE_ADMIN, WS_ROLE_OWNER}

# Session-level recording retention policies.
RETENTION_NONE = "none"          # keep no durable payload (redact eagerly)
RETENTION_7D = "7d"
RETENTION_30D = "30d"
RETENTION_PERMANENT = "permanent"
VALID_RETENTIONS = {RETENTION_NONE, RETENTION_7D, RETENTION_30D, RETENTION_PERMANENT}
# Number of days after which payload is redacted; None => never.
RETENTION_DAYS = {
    RETENTION_NONE: 0,
    RETENTION_7D: 7,
    RETENTION_30D: 30,
    RETENTION_PERMANENT: None,
}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default=ROLE_MEMBER)
    disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    devboxes: Mapped[list["Devbox"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan")


class Devbox(Base):
    __tablename__ = "devbox"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="SET NULL"), nullable=True)

    owner: Mapped[User] = relationship(back_populates="devboxes")
    tokens: Mapped[list["Token"]] = relationship(
        back_populates="devbox", cascade="all, delete-orphan")
    agents: Mapped[list["Agent"]] = relationship(
        back_populates="devbox", cascade="all, delete-orphan")


class Token(Base):
    __tablename__ = "token"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    devbox_id: Mapped[str] = mapped_column(ForeignKey("devbox.id", ondelete="CASCADE"))
    hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    preview: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    devbox: Mapped[Devbox] = relationship(back_populates="tokens")


class Agent(Base):
    __tablename__ = "agent"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    devbox_id: Mapped[str] = mapped_column(ForeignKey("devbox.id", ondelete="CASCADE"))
    handle: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    runtime: Mapped[str] = mapped_column(String, default="mock")  # mock|claude-code|copilot-cli|codex-cli
    cwd: Mapped[str | None] = mapped_column(String, nullable=True)
    launch_cmd: Mapped[str | None] = mapped_column(String, nullable=True)
    presence: Mapped[str] = mapped_column(String, default="offline")  # offline|online|busy|error
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    devbox: Mapped[Devbox] = relationship(back_populates="agents")


class Session(Base):
    __tablename__ = "session"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String, default="Session")
    # Recording retention policy: none|7d|30d|permanent (see VALID_RETENTIONS).
    retention: Mapped[str] = mapped_column(String, default=RETENTION_30D)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Message(Base):
    __tablename__ = "message"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.id", ondelete="CASCADE"))
    author_kind: Mapped[str] = mapped_column(String)  # user|agent|system
    author_id: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class BootstrapState(Base):
    """Singleton row (id=1) that records first-owner bootstrap has occurred.

    Its unique primary key provides a persistent, concurrency-safe atomic
    latch: the first transaction to insert id=1 (together with the owner user)
    wins; any concurrent claim fails the unique constraint and loses.
    """
    __tablename__ = "bootstrap_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="RESTRICT"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Invitation(Base):
    """Single-use invitation. Only the SHA-256 token hash is stored."""
    __tablename__ = "invitation"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    redeemed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    redeemed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class RecordingFrame(Base):
    """Durable Protocol v3 output frame.

    One row per accepted (session_id, pty_instance_id, seq) triple. The unique
    constraint lets SQLite arbitrate concurrent inserts so an ACK is only ever
    sent after a committed row exists. ``payload_hash`` lets a re-sent frame be
    distinguished as an identical duplicate (safe to re-ACK) from a conflicting
    duplicate (same seq, different bytes -> reject).
    """
    __tablename__ = "recording_frame"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "pty_instance_id", "seq",
            name="uq_recording_frame_seq",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), index=True)
    pty_instance_id: Mapped[str] = mapped_column(String, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String, default="o")  # asciicast event code
    data: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String)
    elapsed: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    # Retention: when the payload has been redacted the seq/hash identity row is
    # preserved (Protocol v3 duplicate-ACK ledger) but ``data`` is blanked.
    redacted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class RecordingCheckpoint(Base):
    """Periodic full-screen snapshot enabling O(1) replay seek.

    Keyed by a *durable frame cursor* — the ``RecordingFrame.id`` of the last
    frame folded into ``screen`` — so a checkpoint refers to an unambiguous
    point across interleaved ``pty_instance_id`` streams. Seeking to time T
    means: load the newest checkpoint whose ``frame_id`` <= the target frame,
    restore ``screen``, then replay only the frames after it.
    """
    __tablename__ = "recording_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "frame_id",
            name="uq_recording_checkpoint_frame",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), index=True)
    # Durable frame cursor: RecordingFrame.id of the last applied frame.
    frame_id: Mapped[int] = mapped_column(Integer, index=True)
    # Event ordinal within the merged durable stream (0-based count of frames
    # applied), so a replay client can align the checkpoint to its event list.
    event_index: Mapped[int] = mapped_column(Integer, default=0)
    elapsed: Mapped[float | None] = mapped_column(Float, nullable=True)
    cols: Mapped[int] = mapped_column(Integer, default=80)
    rows: Mapped[int] = mapped_column(Integer, default=24)
    screen: Mapped[str] = mapped_column(Text)  # rendered terminal snapshot
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Organization(Base):
    __tablename__ = "organization"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    is_personal: Mapped[bool] = mapped_column(Integer, default=0)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan")


class Workspace(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String)
    is_personal: Mapped[bool] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    organization: Mapped[Organization] = relationship(back_populates="workspaces")
    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_membership_ws_user"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String, default=WS_ROLE_VIEWER)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")


class SessionParticipant(Base):
    __tablename__ = "session_participant"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "user_id", name="uq_session_participant"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String, default=WS_ROLE_VIEWER)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class KeyboardLease(Base):
    """Exclusive keyboard-control lease for a session (single holder)."""
    __tablename__ = "keyboard_lease"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), unique=True, index=True)
    holder_user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"))
    acquired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, default=1)


SessionLocal: sessionmaker | None = None


def init_db(url: str = "sqlite:///deepbox.db"):
    global _engine, SessionLocal
    _engine = create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("sqlite"):
        _tune_sqlite(_engine)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    _migrate(_engine)
    return _engine


def _tune_sqlite(engine) -> None:
    """Make per-frame commits cheap on Azure Files network storage.

    Every durable output frame commits one RecordingFrame row before the
    server ACKs and broadcasts it to the browser. The SQLite defaults
    (journal_mode=DELETE, synchronous=FULL) force several fsync round-trips per
    commit; on the app's ``/home`` Azure Files share each fsync is a network
    round-trip, which shows up as per-keystroke echo latency because the
    synchronous commit also stalls the event loop.

    WAL + synchronous=NORMAL collapses this to a single sequential append and
    defers the expensive sync to checkpoints, while staying crash-safe: under
    WAL, NORMAL survives OS/process crashes with no corruption (only a power
    loss can lose the last few committed transactions). The connector's durable
    spool remains the source of truth and re-sends any un-ACKed frame on
    reconnect, so even that residual risk is recoverable.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA wal_autocheckpoint=1000")
        finally:
            cur.close()



def _migrate(engine) -> None:
    """Additive migrations for pre-existing SQLite databases.

    Only adds new nullable/defaulted columns; never drops or rewrites data.
    """
    inspector = inspect(engine)
    if "user" not in inspector.get_table_names():
        return
    user_cols = {c["name"] for c in inspector.get_columns("user")}
    stmts: list[str] = []
    if "role" not in user_cols:
        stmts.append(
            f"ALTER TABLE user ADD COLUMN role VARCHAR DEFAULT '{ROLE_MEMBER}'"
        )
    if "disabled_at" not in user_cols:
        stmts.append("ALTER TABLE user ADD COLUMN disabled_at DATETIME")
    if "session" in inspector.get_table_names():
        session_cols = {c["name"] for c in inspector.get_columns("session")}
        if "retention" not in session_cols:
            stmts.append(
                f"ALTER TABLE session ADD COLUMN retention VARCHAR "
                f"DEFAULT '{RETENTION_30D}'"
            )
    if "recording_frame" in inspector.get_table_names():
        frame_cols = {c["name"] for c in inspector.get_columns("recording_frame")}
        if "redacted_at" not in frame_cols:
            stmts.append(
                "ALTER TABLE recording_frame ADD COLUMN redacted_at DATETIME")
    if "devbox" in inspector.get_table_names():
        devbox_cols = {c["name"] for c in inspector.get_columns("devbox")}
        if "workspace_id" not in devbox_cols:
            stmts.append("ALTER TABLE devbox ADD COLUMN workspace_id VARCHAR")
    if "session" in inspector.get_table_names():
        session_cols = {c["name"] for c in inspector.get_columns("session")}
        if "workspace_id" not in session_cols:
            stmts.append("ALTER TABLE session ADD COLUMN workspace_id VARCHAR")
    if "keyboard_lease" in inspector.get_table_names():
        lease_cols = {c["name"] for c in inspector.get_columns("keyboard_lease")}
        if "version" not in lease_cols:
            stmts.append("ALTER TABLE keyboard_lease ADD COLUMN version INTEGER DEFAULT 1")
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    _backfill_workspaces(engine)


def _backfill_workspaces(engine) -> None:
    """Idempotently ensure every Devbox owner has a personal org/workspace and
    every Devbox/Session is assigned a workspace_id."""
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        import uuid as _uuid

        # One personal org/workspace/membership per Devbox owner.
        owner_ws: dict[str, str] = {}
        owner_ids = {
            row[0] for row in session.query(Devbox.owner_user_id).distinct()
        }
        for uid in owner_ids:
            ws = (
                session.query(Workspace)
                .filter(Workspace.is_personal == 1)
                .join(Organization, Workspace.org_id == Organization.id)
                .filter(Organization.owner_user_id == uid)
                .first()
            )
            if ws is None:
                org = Organization(
                    id=str(_uuid.uuid4()), name="Personal",
                    is_personal=1, owner_user_id=uid)
                session.add(org)
                session.flush()
                ws = Workspace(
                    id=str(_uuid.uuid4()), org_id=org.id,
                    name="Personal", is_personal=1)
                session.add(ws)
                session.flush()
            exists = (
                session.query(Membership)
                .filter_by(workspace_id=ws.id, user_id=uid)
                .first()
            )
            if exists is None:
                session.add(Membership(
                    id=str(_uuid.uuid4()), workspace_id=ws.id,
                    user_id=uid, role=WS_ROLE_OWNER))
            owner_ws[uid] = ws.id

        # Backfill Devbox.workspace_id.
        for db in session.query(Devbox).filter(Devbox.workspace_id.is_(None)):
            ws_id = owner_ws.get(db.owner_user_id)
            if ws_id is not None:
                db.workspace_id = ws_id

        session.flush()

        # Backfill Session.workspace_id via agent -> devbox.
        for sess in session.query(Session).filter(Session.workspace_id.is_(None)):
            agent = session.query(Agent).filter_by(id=sess.agent_id).first()
            if agent is None:
                continue
            devbox = session.query(Devbox).filter_by(id=agent.devbox_id).first()
            if devbox is not None and devbox.workspace_id is not None:
                sess.workspace_id = devbox.workspace_id

        session.commit()
    finally:
        session.close()
