import importlib
import os
import tempfile
from unittest.mock import patch


_tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def build_app():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DEEPBOX_")}
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


def create_devbox(client, name="box"):
    response = client.post("/api/devboxes", json={"name": name})
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["devbox"], payload["token"]


def skill_payload(**overrides):
    payload = {
        "id": "skill-1",
        "name": "review-code",
        "description": "Review a change safely.",
        "digest": "a" * 64,
        "scope": "personal",
        "project_id": None,
        "targets": ["claude-code", "copilot-cli", "codex-cli"],
        "status": "installed",
        "contains_scripts": True,
    }
    payload.update(overrides)
    return payload


def test_skill_report_stores_only_sanitized_metadata():
    client, main = build_app()
    with client:
        register(client)
        devbox, token = create_devbox(client)
        auth = {"authorization": f"Bearer {token}"}
        private_path = r"C:\Users\owner\.deepbox\skills\store\secret"
        item = skill_payload(store_path=private_path,
                             bindings={"claude": private_path})

        response = client.post(
            f"/api/devboxes/{devbox['id']}/skills",
            headers=auth, json={"skills": [item]})
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "skills": 1}

        listed = client.get("/api/devboxes")
        assert listed.status_code == 200
        stored = listed.json()[0]["skills"][0]
        assert stored == skill_payload()
        assert private_path not in listed.text
        assert "store_path" not in stored
        assert "bindings" not in stored

        with main.models.SessionLocal() as database:
            row = database.get(main.models.Devbox, devbox["id"])
            assert row.skills == [skill_payload()]

        cleared = client.post(
            f"/api/devboxes/{devbox['id']}/skills",
            headers=auth, json={"skills": []})
        assert cleared.status_code == 200
        assert client.get("/api/devboxes").json()[0]["skills"] == []


def test_skill_report_inventory_limit_is_256():
    client, _ = build_app()
    with client:
        register(client)
        devbox, token = create_devbox(client)
        auth = {"authorization": f"Bearer {token}"}
        skills = [
            skill_payload(id=f"skill-{index}", name=f"skill-{index}")
            for index in range(256)
        ]

        accepted = client.post(
            f"/api/devboxes/{devbox['id']}/skills",
            headers=auth,
            json={"skills": skills},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"ok": True, "skills": 256}

        rejected = client.post(
            f"/api/devboxes/{devbox['id']}/skills",
            headers=auth,
            json={
                "skills": skills + [
                    skill_payload(id="skill-256", name="skill-256")
                ]
            },
        )
        assert rejected.status_code == 422


def test_skill_report_validates_project_scope_identity_and_auth():
    client, _ = build_app()
    with client:
        register(client)
        first, first_token = create_devbox(client, "first")
        second, second_token = create_devbox(client, "second")
        first_auth = {"authorization": f"Bearer {first_token}"}
        second_auth = {"authorization": f"Bearer {second_token}"}

        project = client.post(
            f"/api/devboxes/{first['id']}/projects", headers=first_auth,
            json={"projects": [{"id": "project-1", "name": "Deepbox"}],
                  "migrations": []})
        assert project.status_code == 200, project.text

        accepted = client.post(
            f"/api/devboxes/{first['id']}/skills", headers=first_auth,
            json={"skills": [skill_payload(
                scope="project", project_id="project-1")]})
        assert accepted.status_code == 200, accepted.text

        unknown_project = client.post(
            f"/api/devboxes/{first['id']}/skills", headers=first_auth,
            json={"skills": [skill_payload(
                scope="project", project_id="missing")]})
        assert unknown_project.status_code == 422

        duplicate = client.post(
            f"/api/devboxes/{first['id']}/skills", headers=first_auth,
            json={"skills": [skill_payload(), skill_payload(id="skill-2")]})
        assert duplicate.status_code == 422

        malformed = client.post(
            f"/api/devboxes/{first['id']}/skills", headers=first_auth,
            json={"skills": [skill_payload(digest="not-a-digest")]})
        assert malformed.status_code == 422

        wrong_token = client.post(
            f"/api/devboxes/{first['id']}/skills", headers=second_auth,
            json={"skills": []})
        assert wrong_token.status_code == 403

        removed_project = client.post(
            f"/api/devboxes/{first['id']}/projects", headers=first_auth,
            json={"projects": [], "migrations": []})
        assert removed_project.status_code == 200, removed_project.text
        first_box = next(
            item for item in client.get("/api/devboxes").json()
            if item["id"] == first["id"]
        )
        assert first_box["skills"] == []
