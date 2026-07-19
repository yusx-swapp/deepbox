"""Tests for the post-restart smoke check logic."""
import json
import unittest

from server.ops import smoke


def make_fetch(responses):
    """responses: dict mapping url-suffix -> (status, body)."""
    def fetch(url):
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected url {url}")
    return fetch


class SmokeTests(unittest.TestCase):
    def test_all_pass(self):
        fetch = make_fetch({
            "/api/health": (200, json.dumps({"status": "ok"})),
            "/api/ready": (200, json.dumps({"status": "ready"})),
            "/api/version": (200, json.dumps({"version": "0.1.0", "commit": "abc"})),
        })
        results = smoke.evaluate_smoke(fetch, "http://x")
        self.assertTrue(all(r.ok for r in results))

    def test_health_bad_status(self):
        fetch = make_fetch({
            "/api/health": (200, json.dumps({"status": "degraded"})),
            "/api/ready": (200, "{}"),
            "/api/version": (200, json.dumps({"version": "1"})),
        })
        results = {r.name: r for r in smoke.evaluate_smoke(fetch, "http://x")}
        self.assertFalse(results["health"].ok)

    def test_ready_503(self):
        fetch = make_fetch({
            "/api/health": (200, json.dumps({"status": "ok"})),
            "/api/ready": (503, "not ready"),
            "/api/version": (200, json.dumps({"version": "1"})),
        })
        results = {r.name: r for r in smoke.evaluate_smoke(fetch, "http://x")}
        self.assertFalse(results["ready"].ok)

    def test_ready_requires_ready_payload(self):
        fetch = make_fetch({
            "/api/health": (200, json.dumps({"status": "ok"})),
            "/api/ready": (200, json.dumps({"status": "degraded"})),
            "/api/version": (200, json.dumps({"version": "1"})),
        })
        results = {r.name: r for r in smoke.evaluate_smoke(fetch, "http://x")}
        self.assertFalse(results["ready"].ok)

    def test_version_missing_field(self):
        fetch = make_fetch({
            "/api/health": (200, json.dumps({"status": "ok"})),
            "/api/ready": (200, "{}"),
            "/api/version": (200, json.dumps({})),
        })
        results = {r.name: r for r in smoke.evaluate_smoke(fetch, "http://x")}
        self.assertFalse(results["version"].ok)

    def test_transport_error(self):
        fetch = make_fetch({
            "/api/health": ConnectionError("refused"),
            "/api/ready": (200, "{}"),
            "/api/version": (200, json.dumps({"version": "1"})),
        })
        results = {r.name: r for r in smoke.evaluate_smoke(fetch, "http://x")}
        self.assertFalse(results["health"].ok)

    def test_main_returns_nonzero_on_failure(self):
        # base-url with an unreachable port; transport errors -> exit 1
        rc = smoke.main(["--base-url", "http://127.0.0.1:1", "--timeout", "0.2"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
