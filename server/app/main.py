"""deepbox server — FastAPI app: auth, management REST, runtime REST, and two
WebSocket endpoints (human terminal + devbox connector)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import (
    FastAPI, Request, Response, HTTPException, Depends, WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeSerializer, BadSignature
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as OrmSession

from . import models
from .models import (
    User, Devbox, Token, Agent, Session, Message, BootstrapState, Invitation,
    Organization, Workspace, Membership, SessionParticipant, KeyboardLease,
    PROTOCOL_VERSION, ROLE_OWNER, ROLE_MEMBER, now,
    WS_ROLE_OWNER, WS_ROLE_ADMIN, WS_ROLE_OPERATOR, WS_ROLE_VIEWER,
    VALID_WS_ROLES,
)
from .util import (
    new_id, new_token, hash_token, hash_password, verify_password_ex,
)
from .hub import hub, DevboxConn, HumanConn
from .config import settings
from .live import live_registry
from .recording import RecordingStore, NEW, DUPLICATE, GAP, CONFLICT, INVALID, output_ack_response
from .logging import configure_logging, log_event
from .capacity import collect_capacity, transition_event
from .audit import audit_event
from .collaboration import (
    LeaseConflict, LeaseError, PermissionDenied, acquire_keyboard_lease, can_control,
    get_keyboard_lease, get_role, handoff_keyboard_lease, lease_is_expired,
    list_user_workspaces, release_keyboard_lease, renew_keyboard_lease,
    require_workspace_access, role_at_least,
)
from .security import (
    SAFE_METHODS, RateLimiter, RateLimitRule, build_security_headers,
    is_origin_allowed,
)
from . import version as version_info

import logging as _logging

configure_logging(os.getenv("DEEPBOX_LOG_LEVEL", "INFO"))
logger = _logging.getLogger("deepbox")
_capacity_status = "ok"
_api_limiter = RateLimiter(RateLimitRule(settings.rate_limit_api_per_minute, 60))
_login_limiter = RateLimiter(RateLimitRule(settings.rate_limit_login_per_minute, 60))
_token_limiter = RateLimiter(RateLimitRule(settings.rate_limit_token_per_minute, 60))
_RATE_EXEMPT = {"/api/health", "/api/ready", "/api/version", "/api/auth/bootstrap-status"}


def _durable_events_loader(session_id: str):
    """Load committed v3 frames for a session using a short-lived DB session.

    Used by the LiveRegistry so it never retains a request-scoped Session.
    """
    db = models.SessionLocal()
    try:
        return RecordingStore.durable_events(db, session_id)
    finally:
        db.close()


live_registry.durable_loader = _durable_events_loader
recording_store = RecordingStore()


def observe_capacity(report, *, source: str) -> None:
    """Log only capacity transitions, avoiding one warning per health probe."""

    global _capacity_status
    event = transition_event(_capacity_status, report.status)
    _capacity_status = report.status
    if event is None:
        return
    log_event(
        logger,
        event,
        level=_logging.INFO if report.status == "ok" else _logging.WARNING,
        status=report.status,
        resources=[r.name for r in report.resources if r.status != "ok"],
        source=source,
    )


signer = URLSafeSerializer(settings.secret, salt="deepbox-session")

app = FastAPI(title="deepbox")

@app.middleware("http")
async def security_baseline(request: Request, call_next):
    """Production request guards plus baseline headers on every response."""
    path = request.url.path
    client = request.client.host if request.client else "unknown"
    if settings.rate_limit_enabled and path.startswith("/api/") and path not in _RATE_EXEMPT:
        if path == "/api/auth/login":
            limiter, path_class = _login_limiter, "login"
        elif "/tokens" in path or path.endswith("/devboxes"):
            limiter, path_class = _token_limiter, "credentials"
        else:
            limiter, path_class = _api_limiter, "api"
        decision = limiter.check((client, path_class))
        if not decision.allowed:
            audit_event("http.rate_limited", outcome="denied", request=request,
                        resource_type="route", resource_id=path,
                        details={"retry_after": decision.retry_after})
            response = JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                                    headers={"Retry-After": str(decision.retry_after)})
            response.headers.update(build_security_headers(production=settings.production))
            return response
    # Browser mutation requests authenticated by ambient cookies must prove
    # same-site intent. Bearer connector requests are not cookie-authenticated.
    if (settings.production and request.method.upper() not in SAFE_METHODS
            and request.cookies.get("deepbox_session")
            and not request.headers.get("authorization", "").lower().startswith("bearer ")
            and not is_origin_allowed(request.method, request.headers.get("origin"),
                                      settings.allowed_origins)):
        audit_event("http.csrf_rejected", outcome="denied", request=request,
                    resource_type="route", resource_id=path)
        response = JSONResponse({"detail": "origin not allowed"}, status_code=403)
        response.headers.update(build_security_headers(production=True))
        return response
    response = await call_next(request)
    response.headers.update(build_security_headers(production=settings.production))
    if path.startswith("/api/auth/") or "/tokens" in path:
        response.headers["Cache-Control"] = "no-store"
    elif path == "/" or path.startswith("/static/"):
        # The shell and its dynamically loaded helpers must be one compatible cut.
        # Revalidate on every page load so an App Service deployment cannot mix a
        # fresh app.js with an older cached helper API.
        response.headers["Cache-Control"] = "no-cache"
    return response

models.init_db(settings.database_url)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


# ---------------------------------------------------------------- db dep
def db() -> OrmSession:
    s = models.SessionLocal()
    try:
        yield s
    finally:
        s.close()


@app.get("/api/health", include_in_schema=False)
async def health():
    """Liveness only; intentionally contains no user or infrastructure data."""
    return {"status": "ok", "protocol_version": PROTOCOL_VERSION}


@app.get("/api/ready", include_in_schema=False)
async def ready(s: OrmSession = Depends(db)):
    """Readiness: database responds and the recording directory is writable."""
    try:
        s.execute(text("SELECT 1"))
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(settings.data_dir, os.W_OK):
            raise OSError("data directory is not writable")
    except Exception:
        raise HTTPException(503, "not ready")

    # Azure probes readiness continuously, so this also makes disk-pressure
    # warnings proactive without changing readiness or exposing local paths.
    report = collect_capacity(settings)
    observe_capacity(report, source="readiness_probe")
    return {"status": "ready", "protocol_version": PROTOCOL_VERSION}


@app.get("/api/version", include_in_schema=False)
async def version_public():
    """Public build provenance: marketing version + short commit only.

    Intentionally omits working-tree state and paths so it is safe to expose
    without authentication.
    """
    return version_info.public_version()


@app.get("/api/admin/version", include_in_schema=False)
async def version_detailed(request: Request, s: OrmSession = Depends(db)):
    """Operator build provenance (owner-only): full commit + dirty flag."""
    require_owner(request, s)
    return version_info.detailed_version()


@app.get("/api/admin/capacity", include_in_schema=False)
async def capacity_status(request: Request, s: OrmSession = Depends(db)):
    """Owner-only capacity report for the database and recording disk."""
    require_owner(request, s)
    report = collect_capacity(settings)
    observe_capacity(report, source="admin_api")
    return report.to_dict()


# ---------------------------------------------------------------- auth helpers
def current_user(request: Request, s: OrmSession) -> User:
    cookie = request.cookies.get("deepbox_session")
    if not cookie:
        raise HTTPException(401, "not logged in")
    try:
        data = signer.loads(cookie)
    except BadSignature:
        raise HTTPException(401, "bad session")
    user = s.get(User, data.get("uid"))
    if not user:
        raise HTTPException(401, "user gone")
    if user.disabled_at is not None:
        raise HTTPException(403, "account disabled")
    return user


def require_owner(request: Request, s: OrmSession) -> User:
    u = current_user(request, s)
    if u.role != ROLE_OWNER:
        raise HTTPException(403, "owner only")
    return u


def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _as_utc(value):
    """Normalize SQLite's timezone-naive UTC datetimes for safe comparison."""
    utc = now().tzinfo
    return value.replace(tzinfo=utc) if value.tzinfo is None else value.astimezone(utc)


def devbox_from_bearer(request: Request, s: OrmSession) -> Devbox:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "no bearer token")
    full = auth[7:].strip()
    tok = s.scalar(select(Token).where(Token.hash == hash_token(full)))
    if not tok or tok.revoked_at is not None:
        raise HTTPException(401, "invalid token")
    devbox = s.get(Devbox, tok.devbox_id)
    owner = s.get(User, devbox.owner_user_id) if devbox else None
    if not owner or owner.disabled_at is not None:
        raise HTTPException(401, "invalid token")
    tok.last_used_at = now()
    s.commit()
    return devbox


# ---------------------------------------------------------------- auth routes
@app.post("/api/auth/register")
async def register(request: Request, s: OrmSession = Depends(db)):
    """Development-only self-registration.

    Production keeps DEEPBOX_REGISTRATION_ENABLED=false; invitations are the
    onboarding mechanism there. When an invite code is supplied it is redeemed
    atomically and the created user is a member.
    """
    body = await request.json()
    invite_code = (body.get("invite_code") or "").strip()
    username = body["username"].strip()
    if invite_code:
        return _redeem_invitation(s, invite_code, username, body, request)

    if s.scalar(select(User).where(User.username == username)):
        raise HTTPException(400, "username taken")
    if not settings.registration_enabled:
        raise HTTPException(403, "registration disabled")
    user = User(
        id=new_id(), username=username,
        password_hash=hash_password(body["password"]),
        display_name=body.get("display_name") or username,
        role=ROLE_MEMBER,
    )
    s.add(user)
    s.commit()
    return _login_response(user)


def _redeem_invitation(s: OrmSession, invite_code: str, username: str,
                       body: dict, request: Request) -> JSONResponse:
    # Keep every failed invitation claim opaque, including username conflicts,
    # so an untrusted code cannot be used as an account-enumeration oracle.
    if not username or not body.get("password"):
        raise HTTPException(404, "not found")
    if s.scalar(select(User.id).where(User.username == username)) is not None:
        raise HTTPException(404, "not found")
    token_hash = _hash_secret(invite_code)
    claim_time = now()
    user_id = new_id()
    user = User(
        id=user_id, username=username,
        password_hash=hash_password(body["password"]),
        display_name=body.get("display_name") or username,
        role=ROLE_MEMBER,
    )
    s.add(user)
    s.flush()
    # Atomic single-use redemption: only unredeemed, unrevoked, unexpired rows
    # can be claimed, and the row is stamped in the same conditional UPDATE.
    result = s.execute(
        text(
            "UPDATE invitation SET redeemed_at=:now, redeemed_by=:uid "
            "WHERE token_hash=:th AND redeemed_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > :now"
        ),
        {"now": claim_time, "uid": user_id, "th": token_hash},
    )
    if result.rowcount != 1:
        s.rollback()
        raise HTTPException(404, "not found")
    s.commit()
    audit_event("invitation.redeemed", outcome="success", actor_user_id=user.id,
                request=request, resource_type="invitation",
                details={"username": username})
    return _login_response(user)


@app.post("/api/auth/login")
async def login(request: Request, s: OrmSession = Depends(db)):
    body = await request.json()
    username = body["username"].strip()
    user = s.scalar(select(User).where(User.username == username))
    check = verify_password_ex(body["password"], user.password_hash) if user else None
    if not user or not check or not check.valid or user.disabled_at is not None:
        audit_event("auth.login", outcome="denied", request=request,
                    details={"username": username})
        raise HTTPException(401, "bad credentials")
    if check.replacement:
        user.password_hash = check.replacement
        s.commit()
    audit_event("auth.login", outcome="success", actor_user_id=user.id, request=request)
    return _login_response(user)


def _login_response(user: User) -> JSONResponse:
    resp = JSONResponse({"id": user.id, "username": user.username,
                         "display_name": user.display_name, "role": user.role})
    resp.set_cookie("deepbox_session", signer.dumps({"uid": user.id}),
                    httponly=True, samesite=settings.cookie_samesite,
                    secure=settings.cookie_secure)
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("deepbox_session")
    return resp


@app.get("/api/me/user")
async def me_user(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    return {"id": u.id, "username": u.username, "display_name": u.display_name,
            "role": u.role}


# ---------------------------------------------------------------- bootstrap
def _bootstrap_available(s: OrmSession) -> bool:
    """True only if a bootstrap token is configured, no bootstrap has occurred,
    and no user exists yet."""
    if not settings.bootstrap_token_hash:
        return False
    if s.get(BootstrapState, 1) is not None:
        return False
    if s.scalar(select(User.id).limit(1)) is not None:
        return False
    return True


@app.get("/api/auth/bootstrap-status")
async def bootstrap_status(s: OrmSession = Depends(db)):
    """Safe boolean only — never echoes the token or hash."""
    return {"available": _bootstrap_available(s)}


@app.post("/api/auth/bootstrap")
async def bootstrap(request: Request, s: OrmSession = Depends(db)):
    """Create the first owner exactly once. Token compared by hash; response
    is a generic 404 for any invalid/unavailable case (no token echo/log)."""
    if not settings.bootstrap_token_hash:
        raise HTTPException(404, "not found")
    body = await request.json()
    provided = (body.get("token") or "")
    if not secrets.compare_digest(_hash_secret(provided), settings.bootstrap_token_hash):
        raise HTTPException(404, "not found")
    # Any pre-existing user makes bootstrap unavailable.
    if s.scalar(select(User.id).limit(1)) is not None:
        raise HTTPException(404, "not found")
    username = (body.get("username") or "").strip()
    if not username or not body.get("password"):
        raise HTTPException(404, "not found")
    user = User(
        id=new_id(), username=username,
        password_hash=hash_password(body["password"]),
        display_name=body.get("display_name") or username,
        role=ROLE_OWNER,
    )
    s.add(user)
    s.flush()
    # Insert the singleton latch in the SAME transaction as the owner. The
    # unique primary key (id=1) makes any concurrent claim lose.
    s.add(BootstrapState(id=1, owner_user_id=user.id))
    try:
        s.commit()
    except Exception:
        s.rollback()
        raise HTTPException(404, "not found")
    return _login_response(user)


# ---------------------------------------------------------------- invitations
def _invitation_json(inv: Invitation) -> dict:
    """Safe metadata only — never includes the token or its hash."""
    expired = _as_utc(inv.expires_at) <= now()
    return {
        "id": inv.id,
        "note": inv.note,
        "created_at": _as_utc(inv.created_at).isoformat(),
        "expires_at": _as_utc(inv.expires_at).isoformat(),
        "redeemed_at": _as_utc(inv.redeemed_at).isoformat() if inv.redeemed_at else None,
        "revoked_at": _as_utc(inv.revoked_at).isoformat() if inv.revoked_at else None,
        "status": ("revoked" if inv.revoked_at else
                   "redeemed" if inv.redeemed_at else
                   "expired" if expired else "active"),
    }


@app.post("/api/invitations")
async def create_invitation(request: Request, s: OrmSession = Depends(db)):
    u = require_owner(request, s)
    body = await request.json()
    ttl_hours = int(body.get("ttl_hours") or 24)
    ttl_hours = max(1, min(ttl_hours, 24 * 30))  # bounded TTL: 1h..30d
    import datetime as _dt
    raw = "deepbox_inv_" + secrets.token_hex(24)
    inv = Invitation(
        id=new_id(), token_hash=_hash_secret(raw), created_by=u.id,
        note=(body.get("note") or None),
        expires_at=now() + _dt.timedelta(hours=ttl_hours),
    )
    s.add(inv)
    s.commit()
    audit_event("invitation.created", actor_user_id=u.id,
                resource_type="invitation", resource_id=inv.id,
                details={"ttl_hours": ttl_hours})
    # Plaintext returned exactly once; never stored or logged.
    out = _invitation_json(inv)
    out["token"] = raw
    return out


@app.get("/api/invitations")
async def list_invitations(request: Request, s: OrmSession = Depends(db)):
    require_owner(request, s)
    rows = s.scalars(select(Invitation).order_by(Invitation.created_at.desc())).all()
    return [_invitation_json(i) for i in rows]


@app.delete("/api/invitations/{invitation_id}")
async def revoke_invitation(invitation_id: str, request: Request,
                            s: OrmSession = Depends(db)):
    actor = require_owner(request, s)
    inv = s.get(Invitation, invitation_id)
    if not inv:
        raise HTTPException(404, "not found")
    if inv.revoked_at is None and inv.redeemed_at is None:
        inv.revoked_at = now()
        s.commit()
    audit_event("invitation.revoked", actor_user_id=actor.id,
                resource_type="invitation", resource_id=inv.id)
    return {"ok": True}


# ---------------------------------------------------------------- user mgmt
def _user_json(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name,
            "role": u.role, "disabled": u.disabled_at is not None,
            "disabled_at": u.disabled_at.isoformat() if u.disabled_at else None}


def _enabled_owner_count(s: OrmSession, exclude_id: str | None = None) -> int:
    q = select(User).where(User.role == ROLE_OWNER, User.disabled_at.is_(None))
    return sum(1 for u in s.scalars(q).all() if u.id != exclude_id)


@app.get("/api/users")
async def list_users(request: Request, s: OrmSession = Depends(db)):
    require_owner(request, s)
    rows = s.scalars(select(User).order_by(User.created_at.asc())).all()
    return [_user_json(u) for u in rows]


@app.post("/api/users/{user_id}/disable")
async def disable_user(user_id: str, request: Request, s: OrmSession = Depends(db)):
    actor = require_owner(request, s)
    target = s.get(User, user_id)
    if not target:
        raise HTTPException(404, "not found")
    if target.disabled_at is not None:
        return _user_json(target)
    # Never disable the last enabled owner (covers self-lockout too).
    if target.role == ROLE_OWNER and _enabled_owner_count(s, exclude_id=target.id) == 0:
        raise HTTPException(400, "cannot disable the last enabled owner")
    target.disabled_at = now()
    devbox_ids = set(s.scalars(
        select(Devbox.id).where(Devbox.owner_user_id == target.id)
    ).all())
    s.commit()
    await hub.disconnect_user(target.id, devbox_ids)
    audit_event("user.disabled", actor_user_id=actor.id,
                resource_type="user", resource_id=target.id)
    return _user_json(target)


@app.post("/api/users/{user_id}/enable")
async def enable_user(user_id: str, request: Request, s: OrmSession = Depends(db)):
    actor = require_owner(request, s)
    target = s.get(User, user_id)
    if not target:
        raise HTTPException(404, "not found")
    target.disabled_at = None
    s.commit()
    audit_event("user.enabled", actor_user_id=actor.id,
                resource_type="user", resource_id=target.id)
    return _user_json(target)


# ---------------------------------------------------------------- devbox mgmt
@app.get("/api/workspaces")
async def list_workspaces(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    rows = list_user_workspaces(s, u.id)
    if not rows:
        rows = [_ensure_personal_workspace(s, u)]
        s.commit()
    return [{"id": w.id, "name": w.name, "org_id": w.org_id,
             "role": get_role(s, w.id, u.id), "is_personal": bool(w.is_personal)}
            for w in rows]


@app.post("/api/workspaces")
async def create_workspace(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    body = await request.json()
    name = str(body.get("name") or "Workspace").strip()[:120]
    org = Organization(id=new_id(), name=name, owner_user_id=u.id)
    workspace = Workspace(id=new_id(), org_id=org.id, name=name)
    s.add_all([org, workspace, Membership(id=new_id(), workspace_id=workspace.id,
                                          user_id=u.id, role=WS_ROLE_OWNER)])
    s.commit()
    audit_event("workspace.created", actor_user_id=u.id,
                resource_type="workspace", resource_id=workspace.id)
    return {"id": workspace.id, "name": workspace.name, "role": WS_ROLE_OWNER}


@app.get("/api/workspaces/{workspace_id}/members")
async def list_workspace_members(workspace_id: str, request: Request,
                                 s: OrmSession = Depends(db)):
    u = current_user(request, s)
    _require_workspace(s, u.id, workspace_id)
    rows = s.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                     .where(Membership.workspace_id == workspace_id)
                     .order_by(User.username)).all()
    return [{"user_id": m.user_id, "username": user.username, "role": m.role}
            for m, user in rows]


@app.post("/api/workspaces/{workspace_id}/members")
async def add_workspace_member(workspace_id: str, request: Request,
                               s: OrmSession = Depends(db)):
    actor = current_user(request, s)
    actor_role = _require_workspace(s, actor.id, workspace_id, WS_ROLE_ADMIN)
    body = await request.json()
    role = body.get("role", WS_ROLE_VIEWER)
    if role not in VALID_WS_ROLES or (role == WS_ROLE_OWNER and actor_role != WS_ROLE_OWNER):
        raise HTTPException(422, "invalid role")
    target = s.scalar(select(User).where(User.username == str(body.get("username", "")).strip()))
    if not target:
        raise HTTPException(404, "not found")
    if get_role(s, workspace_id, target.id):
        raise HTTPException(409, "already a member")
    s.add(Membership(id=new_id(), workspace_id=workspace_id, user_id=target.id, role=role))
    s.commit()
    audit_event("workspace.member_added", actor_user_id=actor.id,
                resource_type="workspace", resource_id=workspace_id,
                details={"target_user_id": target.id, "role": role})
    return {"user_id": target.id, "username": target.username, "role": role}


@app.patch("/api/workspaces/{workspace_id}/members/{user_id}")
async def update_workspace_member(workspace_id: str, user_id: str, request: Request,
                                  s: OrmSession = Depends(db)):
    actor = current_user(request, s)
    actor_role = _require_workspace(s, actor.id, workspace_id, WS_ROLE_ADMIN)
    membership = s.scalar(select(Membership).where(
        Membership.workspace_id == workspace_id, Membership.user_id == user_id))
    if not membership:
        raise HTTPException(404, "not found")
    body = await request.json()
    role = body.get("role")
    if role not in VALID_WS_ROLES:
        raise HTTPException(422, "invalid role")
    if (membership.role == WS_ROLE_OWNER or role == WS_ROLE_OWNER) and actor_role != WS_ROLE_OWNER:
        raise HTTPException(403, "owner role required")
    previous = membership.role
    if previous == WS_ROLE_OWNER and role != WS_ROLE_OWNER:
        owners = s.scalar(select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace_id, Membership.role == WS_ROLE_OWNER))
        if owners <= 1:
            raise HTTPException(409, "workspace must keep an owner")
    session_ids = set(s.scalars(select(Session.id).where(
        Session.workspace_id == workspace_id)).all())
    membership.role = role
    s.commit()
    await hub.disconnect_user_sessions(user_id, session_ids)
    audit_event("workspace.role_changed", actor_user_id=actor.id,
                resource_type="workspace", resource_id=workspace_id,
                details={"target_user_id": user_id, "from": previous, "to": role})
    return {"user_id": user_id, "role": role}


@app.delete("/api/workspaces/{workspace_id}/members/{user_id}")
async def remove_workspace_member(workspace_id: str, user_id: str, request: Request,
                                  s: OrmSession = Depends(db)):
    actor = current_user(request, s)
    actor_role = _require_workspace(s, actor.id, workspace_id, WS_ROLE_ADMIN)
    membership = s.scalar(select(Membership).where(
        Membership.workspace_id == workspace_id, Membership.user_id == user_id))
    if not membership:
        raise HTTPException(404, "not found")
    if membership.role == WS_ROLE_OWNER and actor_role != WS_ROLE_OWNER:
        raise HTTPException(403, "owner role required")
    if membership.role == WS_ROLE_OWNER:
        owners = s.scalar(select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace_id, Membership.role == WS_ROLE_OWNER))
        if owners <= 1:
            raise HTTPException(409, "workspace must keep an owner")
    session_ids = set(s.scalars(select(Session.id).where(
        Session.workspace_id == workspace_id)).all())
    s.delete(membership)
    s.commit()
    await hub.disconnect_user_sessions(user_id, session_ids)
    audit_event("workspace.member_removed", actor_user_id=actor.id,
                resource_type="workspace", resource_id=workspace_id,
                details={"target_user_id": user_id})
    return {"ok": True}


def _agent_json(a: Agent) -> dict:
    return {"id": a.id, "handle": a.handle, "display_name": a.display_name,
            "runtime": a.runtime, "cwd": a.cwd, "launch_cmd": a.launch_cmd,
            "presence": "online" if hub.is_agent_online(a.id) else a.presence}


def _ensure_personal_workspace(s: OrmSession, u: User) -> Workspace:
    membership = s.scalar(select(Membership).where(Membership.user_id == u.id)
                          .order_by(Membership.created_at))
    if membership:
        return s.get(Workspace, membership.workspace_id)
    org = Organization(id=new_id(), name=f"{u.username} personal",
                       is_personal=True, owner_user_id=u.id)
    workspace = Workspace(id=new_id(), org_id=org.id, name="Personal",
                          is_personal=True)
    s.add_all([org, workspace, Membership(id=new_id(), workspace_id=workspace.id,
                                          user_id=u.id, role=WS_ROLE_OWNER)])
    s.flush()
    return workspace


def _require_workspace(s: OrmSession, user_id: str, workspace_id: str,
                       minimum: str = WS_ROLE_VIEWER) -> str:
    try:
        return require_workspace_access(s, workspace_id, user_id, minimum)
    except PermissionDenied:
        raise HTTPException(404, "not found")


def _devbox_role(s: OrmSession, user_id: str, d: Devbox,
                 minimum: str = WS_ROLE_VIEWER) -> str:
    if not d.workspace_id:
        if d.owner_user_id == user_id:
            return WS_ROLE_OWNER
        raise HTTPException(404, "not found")
    return _require_workspace(s, user_id, d.workspace_id, minimum)


def _session_role(s: OrmSession, user_id: str, sess: Session,
                  minimum: str = WS_ROLE_VIEWER) -> str:
    if sess.workspace_id:
        return _require_workspace(s, user_id, sess.workspace_id, minimum)
    if sess.user_id == user_id:
        return WS_ROLE_OWNER
    raise HTTPException(404, "not found")


def _lease_json(s: OrmSession, sess: Session, user_id: str, role: str) -> dict:
    lease = get_keyboard_lease(s, sess.id)
    active = bool(lease and not lease_is_expired(lease, now()))
    holder = s.get(User, lease.holder_user_id) if active else None
    return {"type": "collaboration", "session_id": sess.id, "role": role,
            "keyboard": {"holder_user_id": lease.holder_user_id if active else None,
                         "holder_username": holder.username if holder else None,
                         "expires_at": lease.expires_at.isoformat() if active else None,
                         "is_holder": bool(active and lease.holder_user_id == user_id),
                         "can_request": can_control(role)}}


def _devbox_json(d: Devbox) -> dict:
    return {"id": d.id, "name": d.name, "workspace_id": d.workspace_id,
            "online": d.id in hub.devboxes,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            "capabilities": d.capabilities,
            "agents": [_agent_json(a) for a in d.agents]}


@app.post("/api/devboxes")
async def create_devbox(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    body = await request.json()
    workspace_id = body.get("workspace_id")
    if workspace_id:
        _require_workspace(s, u.id, workspace_id, WS_ROLE_ADMIN)
        workspace = s.get(Workspace, workspace_id)
    else:
        workspace = _ensure_personal_workspace(s, u)
    d = Devbox(id=new_id(), owner_user_id=u.id, workspace_id=workspace.id,
               name=body.get("name") or "My Devbox")
    s.add(d)
    full, h, preview = new_token()
    s.add(Token(id=new_id(), devbox_id=d.id, hash=h, preview=preview))
    s.commit()
    audit_event("devbox.created", actor_user_id=u.id,
                resource_type="devbox", resource_id=d.id,
                details={"workspace_id": workspace.id})
    return {"devbox": _devbox_json(d), "token": full, "token_preview": preview}


@app.get("/api/devboxes")
async def list_devboxes(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    workspace_ids = select(Membership.workspace_id).where(Membership.user_id == u.id)
    rows = s.scalars(select(Devbox).where(Devbox.workspace_id.in_(workspace_ids))).all()
    legacy = s.scalars(select(Devbox).where(Devbox.workspace_id.is_(None),
                                             Devbox.owner_user_id == u.id)).all()
    return [_devbox_json(d) for d in [*rows, *legacy]]


@app.delete("/api/devboxes/{devbox_id}")
async def delete_devbox(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, d, WS_ROLE_ADMIN)
    s.delete(d)
    s.commit()
    return {"ok": True}


def _token_json(token: Token) -> dict:
    return {"id": token.id, "preview": token.preview,
            "created_at": token.created_at.isoformat(),
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None}


@app.get("/api/devboxes/{devbox_id}/tokens")
async def list_tokens(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, d, WS_ROLE_ADMIN)
    rows = s.scalars(select(Token).where(Token.devbox_id == d.id)
                     .order_by(Token.created_at.desc())).all()
    return [_token_json(row) for row in rows]


@app.post("/api/devboxes/{devbox_id}/tokens")
async def rotate_token(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, d, WS_ROLE_ADMIN)
    issued_at = now()
    for token in s.scalars(select(Token).where(
            Token.devbox_id == d.id, Token.revoked_at.is_(None))).all():
        token.revoked_at = issued_at
    full, h, preview = new_token()
    token = Token(id=new_id(), devbox_id=d.id, hash=h, preview=preview)
    s.add(token)
    s.commit()
    await hub.disconnect_devbox(d.id)
    audit_event("devbox.token_rotated", actor_user_id=u.id,
                resource_type="devbox", resource_id=d.id)
    return {"token": full, "token_preview": preview, "token_id": token.id}


@app.delete("/api/devboxes/{devbox_id}/tokens/{token_id}")
async def revoke_token(devbox_id: str, token_id: str, request: Request,
                       s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    token = s.get(Token, token_id)
    if not d or not token or token.devbox_id != d.id:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, d, WS_ROLE_ADMIN)
    if token.revoked_at is None:
        token.revoked_at = now()
        s.commit()
        await hub.disconnect_devbox(d.id)
    audit_event("devbox.token_revoked", actor_user_id=u.id,
                resource_type="token", resource_id=token.id,
                details={"devbox_id": d.id})
    return {"ok": True}


@app.post("/api/devboxes/{devbox_id}/agents")
async def create_agent(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, d, WS_ROLE_ADMIN)
    body = await request.json()
    a = Agent(
        id=new_id(), devbox_id=d.id,
        handle=body["handle"], display_name=body.get("display_name") or body["handle"],
        runtime=body.get("runtime", "mock"),
        cwd=body.get("cwd"), launch_cmd=body.get("launch_cmd"),
    )
    s.add(a)
    s.commit()
    return _agent_json(a)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    a = s.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, a.devbox, WS_ROLE_ADMIN)
    s.delete(a)
    s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- sessions
@app.get("/api/agents/{agent_id}/sessions")
async def list_agent_sessions(agent_id: str, request: Request,
                              s: OrmSession = Depends(db)):
    """List resumable sessions newest-first; opening an agent must not silently
    create a new terminal and hide the persisted one."""
    u = current_user(request, s)
    a = s.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, a.devbox)
    rows = s.scalars(select(Session).where(
        Session.agent_id == agent_id
    ).order_by(Session.created_at.desc())).all()
    result = []
    for sess in rows:
        ls = live_registry.get(sess.id)
        state = ("live" if hub.is_session_active(agent_id, sess.id)
                 else "ended" if ls and ls.ended else "inactive")
        result.append({
            "id": sess.id, "agent_id": sess.agent_id, "title": sess.title,
            "created_at": sess.created_at.isoformat(), "state": state,
        })
    return result


@app.post("/api/agents/{agent_id}/sessions")
async def create_session(agent_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    a = s.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "not found")
    _devbox_role(s, u.id, a.devbox, WS_ROLE_OPERATOR)
    sess = Session(id=new_id(), user_id=u.id, agent_id=a.id,
                   workspace_id=a.devbox.workspace_id, title=f"{a.display_name} session")
    s.add(sess)
    s.commit()
    return {"id": sess.id, "agent_id": a.id, "title": sess.title}


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess:
        raise HTTPException(404, "not found")
    _session_role(s, u.id, sess)
    rows = s.scalars(select(Message).where(Message.session_id == session_id)
                     .order_by(Message.created_at)).all()
    return [{"id": m.id, "author_kind": m.author_kind, "author_id": m.author_id,
             "body": m.body, "created_at": m.created_at.isoformat()} for m in rows]


@app.get("/api/sessions/{session_id}/recording")
async def session_recording(session_id: str, request: Request, s: OrmSession = Depends(db)):
    """Return the asciicast v2 DVR recording for replay/audit.

    Merges legacy .cast history with durable Protocol v3 output frames so each
    event appears exactly once, in deterministic order.
    """
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess:
        raise HTTPException(404, "not found")
    _session_role(s, u.id, sess)
    from .live import cast_header, DATA_DIR
    from fastapi.responses import PlainTextResponse
    merged = live_registry.merged_events(session_id)
    cast_path = DATA_DIR / f"{session_id}.cast"
    if not merged and not cast_path.exists():
        raise HTTPException(404, "no recording")
    lines = [json.dumps(cast_header(session_id))]
    for ev in merged:
        lines.append(json.dumps([ev[0], ev[1], ev[2]]))
    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="application/x-asciicast")


@app.get("/api/sessions/{session_id}/replay")
async def session_replay(session_id: str, request: Request,
                         s: OrmSession = Depends(db)):
    """Owner-scoped structured replay payload for the Cut 6 replay UI.

    Returns the asciicast header, the ordered durable event list (redacted
    frames are omitted so redacted payload can never leak), and persisted
    checkpoints keyed by durable frame cursor to support O(1) seek.
    """
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess:
        raise HTTPException(404, "not found")
    _session_role(s, u.id, sess)
    from .live import cast_header

    header = cast_header(session_id)
    cols = header.get("width", 80)
    rows = header.get("height", 24)

    frames = recording_store.read_range(s, session_id)  # non-redacted, by id
    events = []
    last = 0.0
    for idx, f in enumerate(frames):
        t = f.elapsed if f.elapsed is not None else last
        if t < last:
            t = last
        last = t
        events.append({
            "index": idx,
            "frame_id": f.id,
            "time": round(float(t), 6),
            "kind": f.kind or "o",
            "data": f.data,
        })

    checkpoints = []
    for cp in recording_store.checkpoints(s, session_id):
        checkpoints.append({
            "frame_id": cp.frame_id,
            "event_index": cp.event_index,
            "time": round(float(cp.elapsed), 6) if cp.elapsed is not None else None,
            "cols": cp.cols,
            "rows": cp.rows,
            "screen": cp.screen,
        })

    meta = recording_store.metadata(s, session_id)
    return {
        "session_id": session_id,
        "header": header,
        "cols": cols,
        "rows": rows,
        "retention": getattr(sess, "retention", None),
        "duration": round(float(last), 6),
        "event_count": len(events),
        "events": events,
        "checkpoints": checkpoints,
        "metadata": {
            "frame_count": meta["frame_count"],
            "redacted_count": meta["redacted_count"],
            "pty_instance_ids": meta["pty_instance_ids"],
        },
    }


@app.delete("/api/sessions/{session_id}/recording")
async def erase_session_recording(session_id: str, request: Request,
                                  s: OrmSession = Depends(db)):
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess:
        raise HTTPException(404, "not found")
    _session_role(s, u.id, sess, WS_ROLE_ADMIN)
    result = recording_store.secure_erase(s, session_id)
    audit_event("recording.erased", actor_user_id=u.id,
                resource_type="session", resource_id=session_id,
                details={"frame_count": result.frame_count,
                         "newly_redacted": result.newly_redacted,
                         "checkpoints_deleted": result.checkpoints_deleted})
    return {"session_id": result.session_id,
            "frame_count": result.frame_count,
            "newly_redacted": result.newly_redacted,
            "already_redacted": result.already_redacted,
            "checkpoints_deleted": result.checkpoints_deleted}


@app.patch("/api/sessions/{session_id}/retention")
async def update_session_retention(session_id: str, request: Request,
                                   s: OrmSession = Depends(db)):
    """Update a session's recording retention policy and enforce it now."""
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess:
        raise HTTPException(404, "not found")
    _session_role(s, u.id, sess, WS_ROLE_ADMIN)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    retention = body.get("retention") if isinstance(body, dict) else None
    if retention not in models.VALID_RETENTIONS:
        raise HTTPException(422, "retention must be none, 7d, 30d, or permanent")
    redacted = recording_store.set_retention(s, sess, retention)
    audit_event("recording.retention_changed", actor_user_id=u.id,
                resource_type="session", resource_id=session_id,
                details={"retention": retention, "redacted_frames": redacted})
    return {"session_id": session_id, "retention": retention,
            "redacted_frames": redacted}


# ---------------------------------------------------------------- runtime REST (connector)
@app.get("/api/me")
async def me_devbox(request: Request, s: OrmSession = Depends(db)):
    d = devbox_from_bearer(request, s)
    return {"devbox_id": d.id, "name": d.name,
            "protocol_version": PROTOCOL_VERSION,
            "agents": [{"id": a.id, "handle": a.handle, "runtime": a.runtime,
                        "cwd": a.cwd, "launch_cmd": a.launch_cmd}
                       for a in d.agents]}


@app.post("/api/devboxes/{devbox_id}/runtimes")
async def report_runtimes(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    d = devbox_from_bearer(request, s)
    if d.id != devbox_id:
        raise HTTPException(403, "wrong devbox")
    body = await request.json()
    d.capabilities = body.get("capabilities")
    s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- WS: devbox (connector)
@app.websocket("/ws/devbox")
async def ws_devbox(ws: WebSocket):
    token = ws.headers.get("authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    else:
        token = ""  # tokens in query strings leak into URLs/logs; never accept them
    s = models.SessionLocal()
    tok = s.scalar(select(Token).where(Token.hash == hash_token(token)))
    if not tok or tok.revoked_at is not None:
        await ws.close(code=4001)
        s.close()
        return
    d = s.get(Devbox, tok.devbox_id)
    owner = s.get(User, d.owner_user_id) if d else None
    if d is None or not owner or owner.disabled_at is not None:
        await ws.close(code=4001)
        s.close()
        return
    agent_ids = {a.id for a in d.agents}
    d.last_seen_at = now()
    for a in d.agents:
        a.presence = "online"
    s.commit()
    await ws.accept()
    conn = DevboxConn(ws=ws, devbox_id=d.id, agent_ids=agent_ids)
    await hub.add_devbox(conn)
    log_event(logger, "connector.online", devbox_id=d.id,
              agent_count=len(agent_ids))
    await ws.send_json({"type": "hello", "devbox_id": d.id,
                        "agent_ids": list(agent_ids),
                        "protocol_version": PROTOCOL_VERSION})
    try:
        while True:
            frame = await ws.receive_json()
            t = frame.get("type")
            if t == "heartbeat":
                # Liveness ping from the connector. Refresh last_seen and echo an
                # ack so the connector can measure round-trip health.
                dd = s.get(Devbox, d.id)
                if dd:
                    dd.last_seen_at = now()
                    s.commit()
                await ws.send_json({"type": "heartbeat_ack",
                                    "ts": now().isoformat()})
                log_event(logger, "connector.heartbeat",
                          level=_logging.DEBUG, devbox_id=d.id)
            elif t == "output":
                sid = frame.get("session_id")
                if frame.get("seq") is not None or frame.get("pty_instance_id"):
                    # Protocol v3 durable output. Two-phase so the browser sees
                    # keystroke echo without waiting on the network-disk commit:
                    #   classify (in-memory) -> live feed + browser fan-out
                    #   -> durable commit -> ACK (the durability boundary).
                    result = recording_store.classify_output(s, devbox_id=d.id,
                                                              frame=frame)
                    if result.outcome == NEW:
                        pending_row = result.frame
                        # Fan out to the browser FIRST: echo latency must not
                        # include the fsync. Feeding the live screen and
                        # broadcasting are pure in-memory / socket work.
                        ls = live_registry.get_or_create(sid)
                        if frame.get("kind") != "event":
                            # Structured (chat) frames carry a JSON canonical
                            # event in `data`, not terminal bytes; never feed
                            # them into the pyte screen. The browser demuxes on
                            # `kind` and renders a chat surface instead.
                            ls.feed_live_output(frame.get("data", ""))
                        await hub.to_session_humans(sid, frame)
                        # Now make it durable OFF the event loop: a synchronous
                        # SQLite commit (network disk) would otherwise stall the
                        # loop and delay the very fan-out we just enqueued. The
                        # connection processes its frames serially, so `s` is not
                        # used concurrently while this thread runs.
                        commit = await asyncio.to_thread(
                            recording_store.commit_new, s, pending_row)
                        if commit.outcome == NEW:
                            await ws.send_json({
                                "type": "ack", "session_id": sid,
                                "pty_instance_id": frame.get("pty_instance_id"),
                                "seq": frame.get("seq")})
                            # Auto-checkpoint the live screen after durable NEW
                            # output so the replay UI can seek without full
                            # replay. Also off the loop (DB writes).
                            try:
                                from .live import serialize_screen
                                await asyncio.to_thread(
                                    recording_store.maybe_checkpoint,
                                    s, sid, frame=commit.frame,
                                    screen_fn=lambda ls=ls: serialize_screen(ls.screen),
                                    cols=getattr(ls, "cols", 80),
                                    rows=getattr(ls, "rows", 24))
                            except Exception:
                                logger.debug("checkpoint failed", exc_info=True)
                        else:
                            # Lost a durable race (DUPLICATE/CONFLICT/GAP). The
                            # frame was already shown; respond so the connector
                            # can reconcile (re-ACK / fence / resend).
                            await ws.send_json(output_ack_response(
                                commit, session_id=sid,
                                pty_instance_id=frame.get("pty_instance_id"),
                                seq=frame.get("seq")))
                    elif result.outcome == DUPLICATE:
                        # Already durable and identical: re-ACK, do NOT re-feed
                        # or re-broadcast.
                        await ws.send_json(output_ack_response(
                            result, session_id=sid,
                            pty_instance_id=frame.get("pty_instance_id"),
                            seq=frame.get("seq")))
                    elif result.outcome == GAP:
                        await ws.send_json(output_ack_response(
                            result, session_id=sid,
                            pty_instance_id=frame.get("pty_instance_id"),
                            seq=frame.get("seq")))
                    elif result.outcome == CONFLICT:
                        # Forked pty_instance stream: recover via fence rather
                        # than wedging the connector's single-inflight loop.
                        log_event(logger, "recording.conflict", devbox_id=d.id,
                                  session_id=sid, seq=frame.get("seq"))
                        await ws.send_json(output_ack_response(
                            result, session_id=sid,
                            pty_instance_id=frame.get("pty_instance_id"),
                            seq=frame.get("seq")))
                    else:  # INVALID / ownership
                        log_event(logger, "recording.invalid", devbox_id=d.id,
                                  session_id=sid, reason=result.reason)
                        # "seq below persisted frontier" -> recoverable fence;
                        # any other INVALID stays a terminal error.
                        await ws.send_json(output_ack_response(
                            result, session_id=sid,
                            pty_instance_id=frame.get("pty_instance_id"),
                            seq=frame.get("seq")))
                elif sid:
                    # Legacy (< v3) blind output path.
                    data = frame.get("data", "")
                    ls = live_registry.get_or_create(sid)
                    ls.feed_output(data)          # update screen + DVR record
                    await hub.to_session_humans(sid, frame)  # live broadcast

            elif t == "input_ack":
                sid = frame.get("session_id")
                client_input_id = frame.get("client_input_id")
                sess = s.get(Session, sid) if sid else None
                try:
                    client_input_id = str(UUID(str(client_input_id)))
                except (TypeError, ValueError, AttributeError):
                    continue
                if sess and sess.agent_id in conn.agent_ids:
                    ls = live_registry.get(sid)
                    if ls and frame.get("status") == "delivered":
                        ls.acknowledge_input(client_input_id)
                    frame["client_input_id"] = client_input_id
                    await hub.to_session_humans(sid, frame)
            elif t == "exit":
                sid = frame.get("session_id")
                if sid:
                    conn.active_session_ids.discard(sid)
                    ls = live_registry.get(sid)
                    if ls:
                        ls.mark_ended(frame.get("code"))
                    await hub.to_session_humans(sid, frame)
            elif t in ("ready", "presence"):
                sid = frame.get("session_id")
                if sid:
                    if t == "ready":
                        conn.active_session_ids.add(sid)
                    await hub.to_session_humans(sid, frame)
                if t == "presence":
                    a = s.get(Agent, frame.get("agent_id"))
                    if a:
                        a.presence = frame.get("state", "online")
                        s.commit()
            elif t == "sessions":
                conn.active_session_ids = {
                    item["session_id"] for item in frame.get("sessions", [])
                    if item.get("agent_id") in conn.agent_ids and item.get("session_id")
                }
            elif t == "runtimes":
                d2 = s.get(Devbox, d.id)
                d2.capabilities = frame.get("capabilities")
                s.commit()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove_devbox(d.id)
        log_event(logger, "connector.offline", devbox_id=d.id)
        dd = s.get(Devbox, d.id)
        if dd:
            for a in dd.agents:
                a.presence = "offline"
            s.commit()
        s.close()


async def _broadcast_collaboration(s: OrmSession, sess: Session) -> None:
    for watcher in list(hub.session_watchers.get(sess.id, set())):
        role = get_role(s, sess.workspace_id, watcher.user_id) if sess.workspace_id else (
            WS_ROLE_OWNER if sess.user_id == watcher.user_id else None)
        if role:
            try:
                await watcher.ws.send_json(_lease_json(s, sess, watcher.user_id, role))
            except Exception:
                pass


# ---------------------------------------------------------------- WS: human (terminal)
@app.websocket("/ws/term")
async def ws_term(ws: WebSocket):
    # Browser cookies authenticate this socket, so reject cross-origin WS
    # attempts before reading the session cookie.
    if not settings.origin_allowed(ws.headers.get("origin")):
        await ws.close(code=4003)
        return
    cookie = ws.cookies.get("deepbox_session")
    try:
        uid = signer.loads(cookie)["uid"] if cookie else None
    except BadSignature:
        uid = None
    if not uid:
        await ws.close(code=4001)
        return
    _u = models.SessionLocal()
    try:
        _user = _u.get(User, uid)
        if not _user or _user.disabled_at is not None:
            await ws.close(code=4001)
            return
    finally:
        _u.close()
    await ws.accept()
    conn = HumanConn(ws=ws, user_id=uid)
    hub.add_human(conn)
    s = models.SessionLocal()
    try:
        while True:
            frame = await ws.receive_json()
            t = frame.get("type")
            if t in ("attach", "open"):  # 'open' kept for back-compat
                sess = s.get(Session, frame["session_id"])
                try:
                    role = _session_role(s, uid, sess) if sess else None
                except HTTPException:
                    role = None
                if not sess or not role:
                    await ws.send_json({"type": "error", "message": "no such session"})
                    continue
                participant = s.scalar(select(SessionParticipant).where(
                    SessionParticipant.session_id == sess.id,
                    SessionParticipant.user_id == uid))
                if participant:
                    participant.last_seen_at = now()
                    participant.role = role
                else:
                    s.add(SessionParticipant(id=new_id(), session_id=sess.id,
                                             user_id=uid, role=role))
                lease = get_keyboard_lease(s, sess.id)
                if can_control(role) and (not lease or lease_is_expired(lease, now())):
                    acquire_keyboard_lease(s, sess.id, uid, role)
                    audit_event("keyboard.acquired", actor_user_id=uid,
                                resource_type="session", resource_id=sess.id,
                                details={"reason": "initial_attach"})
                s.commit()
                cols = frame.get("cols", 120)
                rows = frame.get("rows", 30)
                # ensure a LiveSession exists (rebuilds screen from .cast if server restarted)
                ls = live_registry.get_or_create(sess.id, cols, rows)
                ls.subscribers.add(conn)
                hub.watch(conn, sess.id, sess.agent_id)
                await _broadcast_collaboration(s, sess)
                # 1) instantly restore the current screen for this viewer
                await ws.send_json({"type": "restore", "session_id": sess.id,
                                    "data": ls.restore_bytes()})
                if ls.ended:
                    await ws.send_json({"type": "status", "session_id": sess.id,
                                        "state": "ended", "code": ls.exit_code})
                    continue
                # 2) ask the connector to ensure the PTY is alive (idempotent)
                ok = await hub.to_devbox(sess.agent_id, {
                    "type": "open", "agent_id": sess.agent_id,
                    "session_id": sess.id, "cols": cols, "rows": rows})
                await ws.send_json({"type": "status", "session_id": sess.id,
                                    "state": "live" if ok else "offline"})
            elif t == "keyboard_acquire":
                sid = frame.get("session_id")
                sess = s.get(Session, sid)
                try:
                    role = _session_role(s, uid, sess, WS_ROLE_OPERATOR) if sess else None
                    acquire_keyboard_lease(s, sid, uid, role)
                    s.commit()
                    audit_event("keyboard.acquired", actor_user_id=uid,
                                resource_type="session", resource_id=sid,
                                details={"reason": "requested"})
                    await _broadcast_collaboration(s, sess)
                except (HTTPException, PermissionDenied, LeaseConflict) as exc:
                    s.rollback()
                    if isinstance(exc, LeaseConflict) and sess:
                        requester = s.get(User, uid)
                        await hub.to_session_humans(sid, {
                            "type": "keyboard_request", "session_id": sid,
                            "requester_user_id": uid,
                            "requester_username": requester.username if requester else "collaborator",
                        })
                        audit_event("keyboard.requested", actor_user_id=uid,
                                    resource_type="session", resource_id=sid)
                    await ws.send_json({"type": "error", "code": "keyboard_busy",
                                        "message": str(getattr(exc, "detail", exc))})
            elif t == "keyboard_renew":
                sid = frame.get("session_id", "")
                sess = s.get(Session, sid)
                try:
                    if not sess:
                        raise PermissionDenied("unknown session")
                    _session_role(s, uid, sess, WS_ROLE_OPERATOR)
                    renew_keyboard_lease(s, sid, uid)
                    await _broadcast_collaboration(s, sess)
                except (HTTPException, LeaseError, PermissionDenied) as exc:
                    s.rollback()
                    await ws.send_json({"type": "error", "code": "keyboard_renew_failed",
                                        "message": str(getattr(exc, "detail", exc))})
            elif t == "keyboard_release":
                sid = frame.get("session_id")
                sess = s.get(Session, sid)
                released = release_keyboard_lease(s, sid, uid) if sess else False
                s.commit()
                if released:
                    audit_event("keyboard.released", actor_user_id=uid,
                                resource_type="session", resource_id=sid)
                    await _broadcast_collaboration(s, sess)
            elif t == "keyboard_handoff":
                sid = frame.get("session_id")
                target_user_id = frame.get("target_user_id")
                sess = s.get(Session, sid)
                try:
                    if not sess:
                        raise PermissionDenied("unknown session")
                    target_role = _session_role(s, target_user_id, sess, WS_ROLE_OPERATOR)
                    handoff_keyboard_lease(s, sid, uid, target_user_id, target_role)
                    audit_event("keyboard.handed_off", actor_user_id=uid,
                                resource_type="session", resource_id=sid,
                                details={"from_user_id": uid, "to_user_id": target_user_id})
                    await _broadcast_collaboration(s, sess)
                except (HTTPException, LeaseError, PermissionDenied) as exc:
                    s.rollback()
                    await ws.send_json({"type": "error", "code": "keyboard_handoff_failed",
                                        "message": str(getattr(exc, "detail", exc))})
            elif t == "input":
                sid = frame.get("session_id")
                agent_id = conn.sessions.get(sid)
                if agent_id:
                    sess = s.get(Session, sid)
                    try:
                        role = _session_role(s, uid, sess, WS_ROLE_OPERATOR)
                    except HTTPException:
                        await ws.send_json({"type": "error", "code": "read_only",
                                            "message": "viewer access is read-only"})
                        continue
                    lease = get_keyboard_lease(s, sid)
                    if not lease or lease.holder_user_id != uid or lease_is_expired(lease, now()):
                        await ws.send_json({"type": "error", "code": "keyboard_lease_required",
                                            "message": "request keyboard control before typing"})
                        if sess:
                            await _broadcast_collaboration(s, sess)
                        continue
                    # Avoid a SQLite write/commit for every keystroke. The browser
                    # renews independently every 20 seconds; the read above still
                    # rejects expired or foreign leases before forwarding input.
                    raw_input_id = frame.get("client_input_id") or str(uuid4())
                    try:
                        client_input_id = str(UUID(str(raw_input_id)))
                    except (TypeError, ValueError, AttributeError):
                        await ws.send_json({"type": "error", "message": "invalid client_input_id"})
                        continue
                    data = frame.get("data", "")
                    if not isinstance(data, str):
                        await ws.send_json({"type": "error", "message": "invalid input data"})
                        continue
                    ls = live_registry.get(sid)
                    if ls:
                        ls.queue_input(client_input_id, data)
                    frame["agent_id"] = agent_id
                    frame["client_input_id"] = client_input_id
                    frame["data"] = data
                    await hub.to_devbox(agent_id, frame)
            elif t == "resize":
                sid = frame.get("session_id")
                agent_id = conn.sessions.get(sid)
                if agent_id:
                    lease = get_keyboard_lease(s, sid)
                    if not lease or lease.holder_user_id != uid or lease_is_expired(lease, now()):
                        continue
                    ls = live_registry.get(sid)
                    if ls:
                        ls.resize(frame.get("cols", 120), frame.get("rows", 30))
                    frame["agent_id"] = agent_id
                    await hub.to_devbox(agent_id, frame)
            elif t in ("detach", "close"):  # viewer leaves; PTY keeps running
                sid = frame.get("session_id")
                ls = live_registry.get(sid)
                if ls:
                    ls.subscribers.discard(conn)
                hub.unwatch(conn, sid)
            elif t == "terminate":  # explicitly end the session (kill the CLI)
                sid = frame.get("session_id")
                agent_id = conn.sessions.get(sid)
                if agent_id:
                    sess = s.get(Session, sid)
                    try:
                        _session_role(s, uid, sess, WS_ROLE_OPERATOR)
                    except HTTPException:
                        await ws.send_json({"type": "error", "message": "terminate not allowed"})
                        continue
                    lease = get_keyboard_lease(s, sid)
                    if not lease or lease.holder_user_id != uid or lease_is_expired(lease, now()):
                        await ws.send_json({"type": "error", "code": "keyboard_lease_required",
                                            "message": "keyboard holder controls termination"})
                        continue
                    await hub.to_devbox(agent_id, {
                        "type": "terminate", "agent_id": agent_id, "session_id": sid})
                    audit_event("session.terminated", actor_user_id=uid,
                                resource_type="session", resource_id=sid)
    except WebSocketDisconnect:
        pass
    finally:
        for sid in list(conn.sessions):
            ls = live_registry.get(sid)
            if ls:
                ls.subscribers.discard(conn)
        hub.remove_human(conn)
        s.close()


# ---------------------------------------------------------------- static web
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    f = WEB_DIR / "index.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    return "<h1>deepbox</h1><p>web/index.html missing</p>"
