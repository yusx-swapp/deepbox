"""Route-level regression tests for the private-alpha security baseline."""
import asyncio
import hashlib
import importlib
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient


_tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def build_app(*, production=False, login_limit=10):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEEPBOX_")}
    dbfile = tempfile.mktemp(suffix=".db", dir=_tmpdir.name)
    env.update({
        "DEEPBOX_DATABASE_URL": f"sqlite:///{dbfile.replace(os.sep, '/')}",
        "DEEPBOX_BOOTSTRAP_TOKEN": "bootstrap-secret",
        "DEEPBOX_ENV": "production" if production else "development",
        "DEEPBOX_SECRET": "test-session-secret-at-least-32-bytes",
        "DEEPBOX_RATE_LIMIT_LOGIN_PER_MINUTE": str(login_limit),
    })
    if production:
        env.update({
            "DEEPBOX_PUBLIC_URL": "https://deepbox.test",
            "DEEPBOX_ALLOWED_ORIGINS": "https://deepbox.test",
            "DEEPBOX_RATE_LIMIT_ENABLED": "true",
            "DEEPBOX_COOKIE_SECURE": "true",
        })
    with patch.dict(os.environ, env, clear=True):
        config_loaded = "server.app.config" in sys.modules
        import server.app.config as config
        if config_loaded:
            importlib.reload(config)
        models_loaded = "server.app.models" in sys.modules
        import server.app.models as models
        if models_loaded:
            importlib.reload(models)
        main_loaded = "server.app.main" in sys.modules
        import server.app.main as main
        if main_loaded:
            importlib.reload(main)
    return main, TestClient(main.app, base_url="https://deepbox.test")


def bootstrap(client):
    response = client.post("/api/auth/bootstrap", json={
        "token": "bootstrap-secret", "username": "owner", "password": "correct horse"
    })
    assert response.status_code == 200
    return response.json()


def test_legacy_password_is_upgraded_on_successful_login():
    main, client = build_app()
    bootstrap(client)
    salt = "legacy-salt"
    legacy = salt + "$" + hashlib.sha256((salt + "correct horse").encode()).hexdigest()
    with main.models.SessionLocal() as session:
        user = session.query(main.User).filter_by(username="owner").one()
        user.password_hash = legacy
        session.commit()
    fresh = TestClient(main.app, base_url="https://deepbox.test")
    response = fresh.post("/api/auth/login", json={
        "username": "owner", "password": "correct horse"
    })
    assert response.status_code == 200
    with main.models.SessionLocal() as session:
        upgraded = session.query(main.User).filter_by(username="owner").one().password_hash
    assert upgraded.startswith("$argon2id$")


def test_security_headers_and_auth_no_store_are_applied():
    _main, client = build_app()
    response = client.get("/api/auth/bootstrap-status")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_shell_and_static_assets_are_always_revalidated():
    _main, client = build_app()
    shell = client.get("/")
    helper = client.get("/static/ui.js")
    assert shell.status_code == 200
    assert helper.status_code == 200
    assert shell.headers["cache-control"] == "no-cache"
    assert helper.headers["cache-control"] == "no-cache"


def test_production_cookie_mutation_requires_allowed_origin():
    _main, client = build_app(production=True)
    bootstrap(client)
    denied = client.post("/api/devboxes", json={"name": "denied"})
    assert denied.status_code == 403
    allowed = client.post(
        "/api/devboxes", json={"name": "allowed"},
        headers={"Origin": "https://deepbox.test"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["strict-transport-security"].startswith("max-age=")


def test_production_login_rate_limit_returns_retry_after():
    _main, owner_client = build_app(production=True, login_limit=2)
    bootstrap(owner_client)
    fresh = TestClient(owner_client.app, base_url="https://deepbox.test")
    for _ in range(2):
        assert fresh.post("/api/auth/login", json={
            "username": "owner", "password": "wrong"
        }).status_code == 401
    limited = fresh.post("/api/auth/login", json={
        "username": "owner", "password": "wrong"
    })
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1


def test_owner_can_securely_erase_recording_payload_and_checkpoints():
    main, client = build_app()
    owner = bootstrap(client)
    with main.models.SessionLocal() as session:
        devbox = main.Devbox(id="erase-box", owner_user_id=owner["id"], name="erase")
        agent = main.Agent(id="erase-agent", devbox_id=devbox.id,
                           handle="erase", display_name="Erase", runtime="opaque")
        run = main.Session(id="erase-session", user_id=owner["id"], agent_id=agent.id,
                           retention="permanent")
        session.add(devbox)
        session.flush()
        session.add(agent)
        session.flush()
        session.add(run)
        session.flush()
        frame = main.models.RecordingFrame(
            session_id=run.id, pty_instance_id="pty", seq=1, kind="o",
            data="secret terminal output", payload_hash="hash",
        )
        session.add(frame)
        session.flush()
        session.add(main.models.RecordingCheckpoint(
            session_id=run.id, frame_id=frame.id, event_index=0,
            rows=24, cols=80, screen="secret checkpoint",
        ))
        session.commit()
    erased = client.delete("/api/sessions/erase-session/recording")
    assert erased.status_code == 200
    assert erased.json()["newly_redacted"] == 1
    assert erased.json()["checkpoints_deleted"] == 1
    with main.models.SessionLocal() as session:
        frame = session.query(main.models.RecordingFrame).filter_by(
            session_id="erase-session").one()
        assert frame.redacted_at is not None
        assert frame.data != "secret terminal output"
        assert session.query(main.models.RecordingCheckpoint).filter_by(
            session_id="erase-session").count() == 0


def test_rotation_revokes_every_prior_token_and_exposes_no_hash():
    main, client = build_app()
    bootstrap(client)
    created = client.post("/api/devboxes", json={"name": "box"}).json()
    devbox_id = created["devbox"]["id"]
    old_plaintext = created["token"]
    rotated = client.post(f"/api/devboxes/{devbox_id}/tokens").json()
    with main.models.SessionLocal() as session:
        rows = session.query(main.Token).filter_by(devbox_id=devbox_id).all()
    active = [row for row in rows if row.revoked_at is None]
    assert len(active) == 1
    assert active[0].id == rotated["token_id"]
    assert all(row.revoked_at is not None for row in rows if row.id != active[0].id)
    listing = client.get(f"/api/devboxes/{devbox_id}/tokens").text
    assert old_plaintext not in listing
    assert all(row.hash not in listing for row in rows)


def test_classify_output_defers_durable_commit_until_commit_new():
    """Two-phase output: classify is a pure in-memory decision; the row only
    becomes durable in commit_new. The hot path relies on this to fan out to
    the browser before paying the (network-disk) commit cost.
    """
    main, client = build_app()
    bootstrap(client)
    created = client.post("/api/devboxes", json={"name": "box"}).json()
    devbox_id = created["devbox"]["id"]
    agent = client.post(f"/api/devboxes/{devbox_id}/agents", json={
        "handle": "shell", "display_name": "Shell", "runtime": "mock",
    }).json()
    session_id = "twophase-session"
    with main.models.SessionLocal() as session:
        user = session.query(main.User).filter_by(username="owner").one()
        session.add(main.Session(
            id=session_id, user_id=user.id, agent_id=agent["id"], title="ack",
        ))
        session.commit()

    store = main.recording_store
    import server.app.recording as recmod
    frame = {
        "session_id": session_id, "pty_instance_id": "pty-1", "seq": 1,
        "kind": "o", "data": "hello", "elapsed": 0.01,
    }
    with main.models.SessionLocal() as s:
        result = store.classify_output(s, devbox_id=devbox_id, frame=frame)
        assert result.outcome == recmod.NEW
        # classify must NOT have persisted anything yet.
        with main.models.SessionLocal() as probe:
            assert probe.query(main.models.RecordingFrame).filter_by(
                session_id=session_id).count() == 0
        # commit_new is the durability boundary.
        commit = store.commit_new(s, result.frame)
        assert commit.outcome == recmod.NEW
    with main.models.SessionLocal() as probe:
        rows = probe.query(main.models.RecordingFrame).filter_by(
            session_id=session_id).all()
        assert len(rows) == 1 and rows[0].seq == 1


def test_hot_path_commits_off_the_event_loop():
    """The durable commit and checkpoint run via asyncio.to_thread so a slow
    network-disk fsync cannot stall the loop that fans keystroke echo out to
    the browser."""
    import inspect
    import server.app.main as main
    src = inspect.getsource(main.ws_devbox)
    norm = " ".join(src.split())
    assert "asyncio.to_thread( recording_store.commit_new" in norm
    assert "asyncio.to_thread( recording_store.maybe_checkpoint" in norm
