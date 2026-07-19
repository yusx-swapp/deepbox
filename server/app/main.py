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
from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from . import models
from .models import (
    User, Devbox, Token, Agent, Session, Message, BootstrapState, Invitation,
    PROTOCOL_VERSION, ROLE_OWNER, ROLE_MEMBER, now,
)
from .util import (
    new_id, new_token, hash_token, hash_password, verify_password,
)
from .hub import hub, DevboxConn, HumanConn
from .config import settings
from .live import live_registry
from .recording import RecordingStore, NEW, DUPLICATE, GAP, CONFLICT, INVALID
from .logging import configure_logging, log_event
from .capacity import collect_capacity, transition_event
from . import version as version_info

import logging as _logging

configure_logging(os.getenv("DEEPBOX_LOG_LEVEL", "INFO"))
logger = _logging.getLogger("deepbox")
_capacity_status = "ok"


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
        return _redeem_invitation(s, invite_code, username, body)

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
                       body: dict) -> JSONResponse:
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
    return _login_response(user)


@app.post("/api/auth/login")
async def login(request: Request, s: OrmSession = Depends(db)):
    body = await request.json()
    user = s.scalar(select(User).where(User.username == body["username"].strip()))
    if not user or not verify_password(body["password"], user.password_hash):
        raise HTTPException(401, "bad credentials")
    if user.disabled_at is not None:
        raise HTTPException(401, "bad credentials")
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
    require_owner(request, s)
    inv = s.get(Invitation, invitation_id)
    if not inv:
        raise HTTPException(404, "not found")
    if inv.revoked_at is None and inv.redeemed_at is None:
        inv.revoked_at = now()
        s.commit()
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
    return _user_json(target)


@app.post("/api/users/{user_id}/enable")
async def enable_user(user_id: str, request: Request, s: OrmSession = Depends(db)):
    require_owner(request, s)
    target = s.get(User, user_id)
    if not target:
        raise HTTPException(404, "not found")
    target.disabled_at = None
    s.commit()
    return _user_json(target)


# ---------------------------------------------------------------- devbox mgmt
def _agent_json(a: Agent) -> dict:
    return {"id": a.id, "handle": a.handle, "display_name": a.display_name,
            "runtime": a.runtime, "cwd": a.cwd, "launch_cmd": a.launch_cmd,
            "presence": "online" if hub.is_agent_online(a.id) else a.presence}


def _devbox_json(d: Devbox) -> dict:
    return {"id": d.id, "name": d.name,
            "online": d.id in hub.devboxes,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            "capabilities": d.capabilities,
            "agents": [_agent_json(a) for a in d.agents]}


@app.post("/api/devboxes")
async def create_devbox(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    body = await request.json()
    d = Devbox(id=new_id(), owner_user_id=u.id, name=body.get("name") or "My Devbox")
    s.add(d)
    full, h, preview = new_token()
    s.add(Token(id=new_id(), devbox_id=d.id, hash=h, preview=preview))
    s.commit()
    return {"devbox": _devbox_json(d), "token": full, "token_preview": preview}


@app.get("/api/devboxes")
async def list_devboxes(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    rows = s.scalars(select(Devbox).where(Devbox.owner_user_id == u.id)).all()
    return [_devbox_json(d) for d in rows]


@app.delete("/api/devboxes/{devbox_id}")
async def delete_devbox(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d or d.owner_user_id != u.id:
        raise HTTPException(404, "not found")
    s.delete(d)
    s.commit()
    return {"ok": True}


@app.post("/api/devboxes/{devbox_id}/tokens")
async def rotate_token(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d or d.owner_user_id != u.id:
        raise HTTPException(404, "not found")
    full, h, preview = new_token()
    s.add(Token(id=new_id(), devbox_id=d.id, hash=h, preview=preview))
    s.commit()
    return {"token": full, "token_preview": preview}


@app.post("/api/devboxes/{devbox_id}/agents")
async def create_agent(devbox_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    d = s.get(Devbox, devbox_id)
    if not d or d.owner_user_id != u.id:
        raise HTTPException(404, "not found")
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
    if not a or a.devbox.owner_user_id != u.id:
        raise HTTPException(404, "not found")
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
    if not a or a.devbox.owner_user_id != u.id:
        raise HTTPException(404, "not found")
    rows = s.scalars(select(Session).where(
        Session.user_id == u.id, Session.agent_id == agent_id
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
    if not a or a.devbox.owner_user_id != u.id:
        raise HTTPException(404, "not found")
    sess = Session(id=new_id(), user_id=u.id, agent_id=a.id,
                   title=f"{a.display_name} session")
    s.add(sess)
    s.commit()
    return {"id": sess.id, "agent_id": a.id, "title": sess.title}


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess or sess.user_id != u.id:
        raise HTTPException(404, "not found")
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
    if not sess or sess.user_id != u.id:
        raise HTTPException(404, "not found")
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
                    # Protocol v3 durable output: persist first, ACK only after
                    # the row is committed. Never blindly accept output.
                    result = recording_store.persist_output(s, devbox_id=d.id,
                                                             frame=frame)
                    if result.outcome == NEW:
                        # Feed the live screen exactly once (no .cast dual-write)
                        # and broadcast to any attached viewers.
                        ls = live_registry.get_or_create(sid)
                        ls.feed_live_output(frame.get("data", ""))
                        await hub.to_session_humans(sid, frame)
                        await ws.send_json({
                            "type": "ack", "session_id": sid,
                            "pty_instance_id": frame.get("pty_instance_id"),
                            "seq": frame.get("seq")})
                    elif result.outcome == DUPLICATE:
                        # Already durable and identical: re-ACK, do NOT re-feed
                        # or re-broadcast.
                        await ws.send_json({
                            "type": "ack", "session_id": sid,
                            "pty_instance_id": frame.get("pty_instance_id"),
                            "seq": frame.get("seq")})
                    elif result.outcome == GAP:
                        await ws.send_json({
                            "type": "resend", "session_id": sid,
                            "pty_instance_id": frame.get("pty_instance_id"),
                            "expected_seq": result.expected_seq})
                    elif result.outcome == CONFLICT:
                        log_event(logger, "recording.conflict", devbox_id=d.id,
                                  session_id=sid, seq=frame.get("seq"))
                        await ws.send_json({
                            "type": "error", "session_id": sid,
                            "message": "conflicting duplicate frame"})
                    else:  # INVALID / ownership
                        log_event(logger, "recording.invalid", devbox_id=d.id,
                                  session_id=sid, reason=result.reason)
                        await ws.send_json({
                            "type": "error", "session_id": sid,
                            "message": result.reason or "invalid output frame"})
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
                if not sess or sess.user_id != uid:
                    await ws.send_json({"type": "error", "message": "no such session"})
                    continue
                cols = frame.get("cols", 120)
                rows = frame.get("rows", 30)
                # ensure a LiveSession exists (rebuilds screen from .cast if server restarted)
                ls = live_registry.get_or_create(sess.id, cols, rows)
                ls.subscribers.add(conn)
                hub.watch(conn, sess.id, sess.agent_id)
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
            elif t == "input":
                sid = frame.get("session_id")
                agent_id = conn.sessions.get(sid)
                if agent_id:
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
                    await hub.to_devbox(agent_id, {
                        "type": "terminate", "agent_id": agent_id, "session_id": sid})
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
