"""SQLAlchemy models + engine/session for deepbox."""
from __future__ import annotations

import datetime as dt
from sqlalchemy import (
    create_engine, String, Text, ForeignKey, DateTime, JSON,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)

PROTOCOL_VERSION = 2


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


_engine = None
SessionLocal: sessionmaker | None = None


def init_db(url: str = "sqlite:///deepbox.db"):
    global _engine, SessionLocal
    _engine = create_engine(url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    return _engine
