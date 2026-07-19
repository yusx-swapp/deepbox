"""Route-level coverage for Cut 8 workspaces and collaboration authorization."""
import importlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


_tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def build_app():
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEEPBOX_")}
    dbfile = tempfile.mktemp(suffix=".db", dir=_tmpdir.name)
    env.update({
        "DEEPBOX_DATABASE_URL": f"sqlite:///{dbfile.replace(os.sep, '/')}",
        "DEEPBOX_REGISTRATION_ENABLED": "true",
        "DEEPBOX_ENV": "test",
    })
    with patch.dict(os.environ, env, clear=True):
        import server.app.config as config
        import server.app.models as models
        import server.app.main as main
        importlib.reload(config)
        importlib.reload(models)
        importlib.reload(main)
        from fastapi.testclient import TestClient
        return TestClient(main.app), main


def register(client, username):
    response = client.post("/api/auth/register", json={
        "username": username, "password": "strong-password"})
    assert response.status_code == 200, response.text


def login(client, username):
    response = client.post("/api/auth/login", json={
        "username": username, "password": "strong-password"})
    assert response.status_code == 200, response.text


def test_workspace_members_share_resources_and_viewer_is_read_only():
    owner, main = build_app()
    register(owner, "owner")
    workspace = owner.get("/api/workspaces").json()[0]
    devbox_response = owner.post("/api/devboxes", json={"name": "shared"})
    assert devbox_response.status_code == 200
    devbox_id = devbox_response.json()["devbox"]["id"]
    agent_response = owner.post(f"/api/devboxes/{devbox_id}/agents", json={
        "handle": "shell", "display_name": "Shell", "runtime": "mock"})
    assert agent_response.status_code == 200
    agent_id = agent_response.json()["id"]

    register(owner, "viewer")
    login(owner, "owner")
    added = owner.post(f"/api/workspaces/{workspace['id']}/members", json={
        "username": "viewer", "role": "viewer"})
    assert added.status_code == 200

    viewer = owner.__class__(main.app)
    login(viewer, "viewer")
    assert [d["id"] for d in viewer.get("/api/devboxes").json()] == [devbox_id]
    assert viewer.get(f"/api/agents/{agent_id}/sessions").status_code == 200
    assert viewer.post(f"/api/agents/{agent_id}/sessions").status_code == 404
    assert viewer.post(f"/api/devboxes/{devbox_id}/agents", json={
        "handle": "blocked", "display_name": "Blocked"}).status_code == 404


def test_operator_can_create_session_and_viewer_websocket_is_read_only():
    client, main = build_app()
    register(client, "owner")
    workspace_id = client.get("/api/workspaces").json()[0]["id"]
    devbox_id = client.post("/api/devboxes", json={"name": "shared"}).json()["devbox"]["id"]
    agent_id = client.post(f"/api/devboxes/{devbox_id}/agents", json={
        "handle": "shell", "display_name": "Shell", "runtime": "mock"}).json()["id"]
    register(client, "operator")
    register(client, "viewer")
    login(client, "owner")
    assert client.post(f"/api/workspaces/{workspace_id}/members", json={
        "username": "operator", "role": "operator"}).status_code == 200
    assert client.post(f"/api/workspaces/{workspace_id}/members", json={
        "username": "viewer", "role": "viewer"}).status_code == 200

    operator = client.__class__(main.app)
    login(operator, "operator")
    created = operator.post(f"/api/agents/{agent_id}/sessions")
    assert created.status_code == 200
    session_id = created.json()["id"]

    viewer = client.__class__(main.app)
    login(viewer, "viewer")
    with viewer.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "attach", "session_id": session_id, "cols": 80, "rows": 24})
        frames = [ws.receive_json() for _ in range(3)]
        collaboration = next(frame for frame in frames if frame["type"] == "collaboration")
        assert collaboration["role"] == "viewer"
        assert collaboration["keyboard"]["can_request"] is False
        ws.send_json({"type": "input", "session_id": session_id, "data": "whoami\n"})
        denied = ws.receive_json()
        assert denied["type"] == "error"
        assert denied["code"] == "read_only"


def test_only_owner_can_promote_an_owner_and_last_owner_cannot_leave():
    client, _ = build_app()
    register(client, "owner")
    register(client, "admin")
    login(client, "owner")
    workspace_id = client.get("/api/workspaces").json()[0]["id"]
    user_id = client.post(f"/api/workspaces/{workspace_id}/members", json={
        "username": "admin", "role": "admin"}).json()["user_id"]
    assert client.patch(f"/api/workspaces/{workspace_id}/members/{user_id}", json={
        "role": "owner"}).status_code == 200

    admin = client.__class__(client.app)
    login(admin, "admin")
    members = admin.get(f"/api/workspaces/{workspace_id}/members").json()
    original_owner = next(m for m in members if m["username"] == "owner")
    # The promoted owner may manage owners, but the workspace can never lose its final owner.
    assert admin.delete(
        f"/api/workspaces/{workspace_id}/members/{original_owner['user_id']}").status_code == 200
    assert admin.patch(f"/api/workspaces/{workspace_id}/members/{user_id}", json={
        "role": "viewer"}).status_code == 409
    assert admin.delete(f"/api/workspaces/{workspace_id}/members/{user_id}").status_code == 409
