"""deepbox server — FastAPI app: auth, management REST, runtime REST, and two
WebSocket endpoints (human terminal + devbox connector)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import (
    FastAPI, Request, Response, HTTPException, Depends, WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeSerializer, BadSignature
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from . import models
from .models import (
    User, Devbox, Token, Agent, Session, Message, PROTOCOL_VERSION, now,
)
from .util import (
    new_id, new_token, hash_token, hash_password, verify_password,
)
from .hub import hub, DevboxConn, HumanConn
from .live import live_registry

SECRET = "dev-secret-change-me"
signer = URLSafeSerializer(SECRET, salt="deepbox-session")

app = FastAPI(title="deepbox")
models.init_db()

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


# ---------------------------------------------------------------- db dep
def db() -> OrmSession:
    s = models.SessionLocal()
    try:
        yield s
    finally:
        s.close()


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
    return user


def devbox_from_bearer(request: Request, s: OrmSession) -> Devbox:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "no bearer token")
    full = auth[7:].strip()
    tok = s.scalar(select(Token).where(Token.hash == hash_token(full)))
    if not tok or tok.revoked_at is not None:
        raise HTTPException(401, "invalid token")
    tok.last_used_at = now()
    s.commit()
    return s.get(Devbox, tok.devbox_id)


# ---------------------------------------------------------------- auth routes
@app.post("/api/auth/register")
async def register(request: Request, s: OrmSession = Depends(db)):
    body = await request.json()
    username = body["username"].strip()
    if s.scalar(select(User).where(User.username == username)):
        raise HTTPException(400, "username taken")
    user = User(
        id=new_id(), username=username,
        password_hash=hash_password(body["password"]),
        display_name=body.get("display_name") or username,
    )
    s.add(user)
    s.commit()
    return _login_response(user)


@app.post("/api/auth/login")
async def login(request: Request, s: OrmSession = Depends(db)):
    body = await request.json()
    user = s.scalar(select(User).where(User.username == body["username"].strip()))
    if not user or not verify_password(body["password"], user.password_hash):
        raise HTTPException(401, "bad credentials")
    return _login_response(user)


def _login_response(user: User) -> JSONResponse:
    resp = JSONResponse({"id": user.id, "username": user.username,
                         "display_name": user.display_name})
    resp.set_cookie("deepbox_session", signer.dumps({"uid": user.id}),
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("deepbox_session")
    return resp


@app.get("/api/me/user")
async def me_user(request: Request, s: OrmSession = Depends(db)):
    u = current_user(request, s)
    return {"id": u.id, "username": u.username, "display_name": u.display_name}


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
    """Return the asciicast v2 DVR recording for replay/audit."""
    u = current_user(request, s)
    sess = s.get(Session, session_id)
    if not sess or sess.user_id != u.id:
        raise HTTPException(404, "not found")
    from .live import DATA_DIR
    from fastapi.responses import PlainTextResponse
    path = DATA_DIR / f"{session_id}.cast"
    if not path.exists():
        raise HTTPException(404, "no recording")
    return PlainTextResponse(path.read_text(encoding="utf-8"),
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
        token = ws.query_params.get("token", "")
    s = models.SessionLocal()
    tok = s.scalar(select(Token).where(Token.hash == hash_token(token)))
    if not tok or tok.revoked_at is not None:
        await ws.close(code=4001)
        s.close()
        return
    d = s.get(Devbox, tok.devbox_id)
    agent_ids = {a.id for a in d.agents}
    d.last_seen_at = now()
    for a in d.agents:
        a.presence = "online"
    s.commit()
    await ws.accept()
    conn = DevboxConn(ws=ws, devbox_id=d.id, agent_ids=agent_ids)
    await hub.add_devbox(conn)
    await ws.send_json({"type": "hello", "devbox_id": d.id,
                        "agent_ids": list(agent_ids),
                        "protocol_version": PROTOCOL_VERSION})
    try:
        while True:
            frame = await ws.receive_json()
            t = frame.get("type")
            if t == "output":
                sid = frame.get("session_id")
                data = frame.get("data", "")
                if sid:
                    # Create even when no viewer is attached. This is essential
                    # after a server restart: connector drains output buffered
                    # during downtime before a browser necessarily reconnects.
                    ls = live_registry.get_or_create(sid)
                    ls.feed_output(data)          # update screen + DVR record
                    await hub.to_session_humans(sid, frame)  # live broadcast
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
        dd = s.get(Devbox, d.id)
        if dd:
            for a in dd.agents:
                a.presence = "offline"
            s.commit()
        s.close()


# ---------------------------------------------------------------- WS: human (terminal)
@app.websocket("/ws/term")
async def ws_term(ws: WebSocket):
    cookie = ws.cookies.get("deepbox_session")
    try:
        uid = signer.loads(cookie)["uid"] if cookie else None
    except BadSignature:
        uid = None
    if not uid:
        await ws.close(code=4001)
        return
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
                    ls = live_registry.get(sid)
                    if ls:
                        ls.record_input(frame.get("data", ""))
                    frame["agent_id"] = agent_id
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
