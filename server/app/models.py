"""SQLAlchemy models + engine/session for deepbox."""
from __future__ import annotations

import datetime as dt
from sqlalchemy import (
    create_engine, String, Text, ForeignKey, DateTime, JSON, Integer, Float,
    UniqueConstraint, inspect, text,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)

PROTOCOL_VERSION = 3

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
VALID_ROLES = {ROLE_OWNER, ROLE_MEMBER}


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


_engine = None
SessionLocal: sessionmaker | None = None


def init_db(url: str = "sqlite:///deepbox.db"):
    global _engine, SessionLocal
    _engine = create_engine(url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    _migrate(_engine)
    return _engine


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
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
