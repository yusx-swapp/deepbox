"""Agent mutation lifecycle coverage for live connector reconciliation."""
import importlib
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


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


def register(client):
    response = client.post("/api/auth/register", json={
        "username": "owner", "password": "strong-password"})
    assert response.status_code == 200, response.text


def test_concurrent_agent_adds_and_delete_reconcile_live_connector():
    client, main = build_app()
    with client:
        register(client)
        created = client.post("/api/devboxes", json={"name": "box"})
        assert created.status_code == 200
        payload = created.json()
        devbox = payload["devbox"]
        auth = {"authorization": f"Bearer {payload['token']}"}

        with client.websocket_connect("/ws/devbox", headers=auth) as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json() == {"type": "agents", "agents": []}

            def add(handle, runtime):
                return client.post(
                    f"/api/devboxes/{devbox['id']}/agents",
                    json={
                        "handle": handle,
                        "display_name": handle,
                        "runtime": runtime,
                        "cwd": None,
                    },
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(add, "claude", "claude-code"),
                    pool.submit(add, "codex", "codex-cli"),
                ]
                responses = [future.result(timeout=5) for future in futures]

            assert [response.status_code for response in responses] == [200, 200]
            added = [response.json() for response in responses]
            directories = [ws.receive_json(), ws.receive_json()]
            assert all(frame["type"] == "agents" for frame in directories)
            assert {agent["id"] for agent in directories[-1]["agents"]} == {
                agent["id"] for agent in added
            }

            doomed, survivor = added
            session = client.post(f"/api/agents/{doomed['id']}/sessions")
            assert session.status_code == 200
            deleted = client.delete(f"/api/agents/{doomed['id']}")
            assert deleted.status_code == 200

            final_directory = ws.receive_json()
            assert final_directory["type"] == "agents"
            assert [agent["id"] for agent in final_directory["agents"]] == [
                survivor["id"]
            ]
            listed = client.get("/api/devboxes")
            assert listed.status_code == 200
            assert [agent["id"] for agent in listed.json()[0]["agents"]] == [
                survivor["id"]
            ]
            with main.models.SessionLocal() as db:
                assert db.get(main.models.Session, session.json()["id"]) is None
