"""Route tests for ops endpoints: version, capacity, ready."""
import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def build_app(extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEEPBOX_")}
    dbfile = tempfile.mktemp(suffix=".db", dir=_tmpdir.name)
    env["DEEPBOX_DATABASE_URL"] = f"sqlite:///{dbfile.replace(os.sep, '/')}"
    env["DEEPBOX_DATA_DIR"] = tempfile.mkdtemp(dir=_tmpdir.name)
    env["DEEPBOX_GIT_COMMIT"] = "abc123def4567890"
    env["DEEPBOX_BOOTSTRAP_TOKEN"] = "boot-secret-token"
    if extra_env:
        env.update(extra_env)
    with patch.dict(os.environ, env, clear=True):
        assert os.environ.get("DEEPBOX_BOOTSTRAP_TOKEN") == "boot-secret-token", "token missing pre-reload"
        import server.app.config as config
        # Importing config the first time runs load_settings(), which clears the
        # bootstrap token from the environment by design. Re-set it so the
        # reload below re-derives the bootstrap hash.
        os.environ["DEEPBOX_BOOTSTRAP_TOKEN"] = "boot-secret-token"
        importlib.reload(config)
        import server.app.version as version
        version.git_commit.cache_clear()
        version.git_dirty.cache_clear()
        import server.app.main as main
        importlib.reload(main)
        from fastapi.testclient import TestClient
        return TestClient(main.app), main


def register_owner(client):
    r = client.post("/api/auth/bootstrap", json={
        "token": "boot-secret-token", "username": "owner", "password": "s3cret-pw"})
    assert r.status_code == 200, r.text
    return r


class VersionRouteTests(unittest.TestCase):
    def test_public_version_no_auth(self):
        client, _ = build_app()
        r = client.get("/api/version")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(set(data.keys()), {"version", "commit"})
        self.assertNotIn("dirty", data)

    def test_detailed_version_requires_owner(self):
        client, _ = build_app()
        r = client.get("/api/admin/version")
        self.assertIn(r.status_code, (401, 403))
        register_owner(client)
        r = client.get("/api/admin/version")
        self.assertEqual(r.status_code, 200)
        self.assertIn("dirty", r.json())


class CapacityRouteTests(unittest.TestCase):
    def test_capacity_requires_owner(self):
        client, _ = build_app()
        r = client.get("/api/admin/capacity")
        self.assertIn(r.status_code, (401, 403))

    def test_capacity_ok(self):
        client, _ = build_app({"DEEPBOX_DISK_FREE_WARN_MB": "0", "DEEPBOX_DISK_FREE_ALERT_MB": "0"})
        register_owner(client)
        r = client.get("/api/admin/capacity")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("status", data)
        self.assertIn("resources", data)


class ReadyRouteTests(unittest.TestCase):
    def test_ready_ok(self):
        client, _ = build_app()
        r = client.get("/api/ready")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ready")
        # No secrets or filesystem paths leak in the public body.
        self.assertNotIn("secret", r.text.lower())
        self.assertNotIn("data_dir", r.text.lower())

    def test_ready_probe_emits_proactive_capacity_warning(self):
        client, main = build_app()
        warning = SimpleNamespace(
            status="warn",
            resources=(SimpleNamespace(name="database", status="warn"),),
        )
        with patch.object(main, "collect_capacity", return_value=warning), \
                patch.object(main, "log_event") as emit:
            r = client.get("/api/ready")
            repeated = client.get("/api/ready")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(r.json()["status"], "ready")
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[1], "capacity.threshold")
        self.assertEqual(emit.call_args.kwargs["source"], "readiness_probe")


if __name__ == "__main__":
    unittest.main()
