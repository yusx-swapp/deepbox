"""Cut 8 collaboration: pure authorization / lease service.

This module contains no HTTP or transport concerns. It operates on a
SQLAlchemy ``Session`` passed in by the caller so it is trivially testable and
reusable from the hub/router layers.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session as DbSession

from .models import (
    KeyboardLease,
    Membership,
    Workspace,
    WS_ROLE_ADMIN,
    WS_ROLE_OPERATOR,
    WS_ROLE_OWNER,
    WS_ROLE_VIEWER,
)

# Least -> most privileged. Higher index == more privilege.
ROLE_ORDER = [WS_ROLE_VIEWER, WS_ROLE_OPERATOR, WS_ROLE_ADMIN, WS_ROLE_OWNER]
_ROLE_RANK = {role: i for i, role in enumerate(ROLE_ORDER)}

# Roles allowed to acquire the keyboard lease (viewers are read-only).
_CONTROL_ROLES = {WS_ROLE_OPERATOR, WS_ROLE_ADMIN, WS_ROLE_OWNER}

DEFAULT_LEASE_TTL = dt.timedelta(seconds=60)


class LeaseError(Exception):
    """Base class for lease-related errors."""


class LeaseConflict(LeaseError):
    """Another holder currently owns an unexpired lease."""


class PermissionDenied(LeaseError):
    """The user's role does not permit the requested action."""


def _now(now: dt.datetime | None) -> dt.datetime:
    return now if now is not None else dt.datetime.utcnow()


def _utc_naive(value: dt.datetime) -> dt.datetime:
    """Normalize SQLite-naive and timezone-aware timestamps for comparison."""
    if value.tzinfo is None:
        return value
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None)


# --- role ordering ---------------------------------------------------------

def role_rank(role: str) -> int:
    """Return the privilege rank of a role (higher == more privileged)."""
    try:
        return _ROLE_RANK[role]
    except KeyError:
        raise ValueError(f"unknown role: {role!r}")


def role_at_least(role: str, minimum: str) -> bool:
    """True if ``role`` has at least ``minimum`` privilege."""
    return role_rank(role) >= role_rank(minimum)


def can_control(role: str) -> bool:
    """True if the role may acquire the keyboard lease."""
    return role in _CONTROL_ROLES


# --- workspace access helpers ---------------------------------------------

def get_membership(
    db: DbSession, workspace_id: str, user_id: str
) -> Membership | None:
    return (
        db.query(Membership)
        .filter_by(workspace_id=workspace_id, user_id=user_id)
        .one_or_none()
    )


def get_role(db: DbSession, workspace_id: str, user_id: str) -> str | None:
    m = get_membership(db, workspace_id, user_id)
    return m.role if m is not None else None


def has_workspace_access(
    db: DbSession, workspace_id: str, user_id: str, minimum: str = WS_ROLE_VIEWER
) -> bool:
    role = get_role(db, workspace_id, user_id)
    if role is None:
        return False
    return role_at_least(role, minimum)


def require_workspace_access(
    db: DbSession, workspace_id: str, user_id: str, minimum: str = WS_ROLE_VIEWER
) -> str:
    """Return the user's role if it meets ``minimum`` else raise."""
    role = get_role(db, workspace_id, user_id)
    if role is None or not role_at_least(role, minimum):
        raise PermissionDenied(
            f"user {user_id!r} lacks {minimum!r} on workspace {workspace_id!r}")
    return role


def list_user_workspaces(db: DbSession, user_id: str) -> list[Workspace]:
    return (
        db.query(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .filter(Membership.user_id == user_id)
        .all()
    )


# --- keyboard lease --------------------------------------------------------

def get_keyboard_lease(db: DbSession, session_id: str) -> KeyboardLease | None:
    return (
        db.query(KeyboardLease)
        .filter_by(session_id=session_id)
        .one_or_none()
    )


def lease_is_expired(
    lease: KeyboardLease, at: dt.datetime | None = None
) -> bool:
    """Return expiration state across SQLite's naive datetime round-trips."""
    return _utc_naive(lease.expires_at) <= _utc_naive(_now(at))


def _is_expired(lease: KeyboardLease, now: dt.datetime) -> bool:
    return lease_is_expired(lease, now)


def acquire_keyboard_lease(
    db: DbSession,
    session_id: str,
    user_id: str,
    role: str,
    *,
    ttl: dt.timedelta = DEFAULT_LEASE_TTL,
    now: dt.datetime | None = None,
) -> KeyboardLease:
    """Acquire (or renew) the exclusive keyboard lease for a session.

    - viewers cannot acquire (``PermissionDenied``)
    - if no lease or the existing lease is expired -> (re)acquire
    - if the same holder already holds it -> renew
    - if a different holder holds an unexpired lease -> ``LeaseConflict``
    """
    if not can_control(role):
        raise PermissionDenied(f"role {role!r} may not acquire keyboard lease")

    now = _now(now)
    lease = get_keyboard_lease(db, session_id)

    if lease is None:
        lease = KeyboardLease(
            id=str(uuid.uuid4()),
            session_id=session_id,
            holder_user_id=user_id,
            acquired_at=now,
            expires_at=now + ttl,
        )
        db.add(lease)
        db.commit()
        return lease

    if lease.holder_user_id == user_id:
        # same holder -> renew
        lease.acquired_at = now
        lease.expires_at = now + ttl
        db.commit()
        return lease

    if _is_expired(lease, now):
        # preempt expired lease
        lease.holder_user_id = user_id
        lease.acquired_at = now
        lease.expires_at = now + ttl
        db.commit()
        return lease

    raise LeaseConflict(
        f"lease for session {session_id!r} held by {lease.holder_user_id!r}")


def renew_keyboard_lease(
    db: DbSession,
    session_id: str,
    user_id: str,
    *,
    ttl: dt.timedelta = DEFAULT_LEASE_TTL,
    now: dt.datetime | None = None,
) -> KeyboardLease:
    """Extend an existing lease held by ``user_id``."""
    now = _now(now)
    lease = get_keyboard_lease(db, session_id)
    if lease is None:
        raise LeaseError(f"no lease for session {session_id!r}")
    if lease.holder_user_id != user_id:
        raise LeaseConflict(
            f"lease held by {lease.holder_user_id!r}, not {user_id!r}")
    if _is_expired(lease, now):
        raise LeaseError("lease already expired")
    lease.acquired_at = now
    lease.expires_at = now + ttl
    db.commit()
    return lease


def handoff_keyboard_lease(
    db: DbSession,
    session_id: str,
    current_holder_id: str,
    target_user_id: str,
    target_role: str,
    *,
    ttl: dt.timedelta = DEFAULT_LEASE_TTL,
    now: dt.datetime | None = None,
) -> KeyboardLease:
    """Atomically transfer an unexpired lease to another controller."""
    if not can_control(target_role):
        raise PermissionDenied(f"role {target_role!r} may not receive keyboard lease")
    now = _now(now)
    lease = get_keyboard_lease(db, session_id)
    if lease is None or _is_expired(lease, now):
        raise LeaseError(f"no active lease for session {session_id!r}")
    if lease.holder_user_id != current_holder_id:
        raise LeaseConflict(
            f"lease held by {lease.holder_user_id!r}, not {current_holder_id!r}")
    version = lease.version
    result = db.execute(
        update(KeyboardLease)
        .where(KeyboardLease.session_id == session_id,
               KeyboardLease.holder_user_id == current_holder_id,
               KeyboardLease.version == version)
        .values(holder_user_id=target_user_id, acquired_at=now,
                expires_at=now + ttl, version=version + 1)
    )
    if result.rowcount != 1:
        db.rollback()
        raise LeaseConflict("lease changed during handoff")
    db.commit()
    return get_keyboard_lease(db, session_id)


def release_keyboard_lease(
    db: DbSession,
    session_id: str,
    user_id: str,
) -> bool:
    """Release the lease if held by ``user_id``. Returns True if released."""
    lease = get_keyboard_lease(db, session_id)
    if lease is None:
        return False
    if lease.holder_user_id != user_id:
        raise LeaseConflict(
            f"lease held by {lease.holder_user_id!r}, not {user_id!r}")
    db.delete(lease)
    db.commit()
    return True
