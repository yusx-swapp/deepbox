"""Tests for version/build-provenance reporting."""
import os
import unittest
from unittest.mock import patch

from server.app import version


class VersionTests(unittest.TestCase):
    def tearDown(self):
        version.git_commit.cache_clear()
        version.git_dirty.cache_clear()

    def test_env_override_wins(self):
        with patch.dict(os.environ, {"DEEPBOX_GIT_COMMIT": "abc123def4567890"}):
            version.git_commit.cache_clear()
            self.assertEqual(version.git_commit(), "abc123def4567890")
            self.assertEqual(version.short_commit(), "abc123def456")

    def test_dirty_false_in_artifact(self):
        with patch.dict(os.environ, {"DEEPBOX_GIT_COMMIT": "deadbeef"}):
            version.git_dirty.cache_clear()
            self.assertFalse(version.git_dirty())

    def test_public_hides_dirty(self):
        with patch.dict(os.environ, {"DEEPBOX_GIT_COMMIT": "abc123def4567890"}):
            version.git_commit.cache_clear()
            pub = version.public_version()
            self.assertEqual(set(pub.keys()), {"version", "commit"})
            self.assertNotIn("dirty", pub)

    def test_detailed_has_operator_fields(self):
        with patch.dict(os.environ, {"DEEPBOX_GIT_COMMIT": "abc123def4567890"}):
            version.git_commit.cache_clear()
            version.git_dirty.cache_clear()
            det = version.detailed_version()
            self.assertIn("dirty", det)
            self.assertIn("commit_short", det)
            self.assertEqual(det["version"], version.VERSION)

    def test_unknown_when_no_git(self):
        env = {k: v for k, v in os.environ.items() if k != "DEEPBOX_GIT_COMMIT"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(version, "_run_git", return_value=None):
                version.git_commit.cache_clear()
                self.assertEqual(version.git_commit(), "unknown")
                self.assertEqual(version.short_commit(), "unknown")


if __name__ == "__main__":
    unittest.main()
