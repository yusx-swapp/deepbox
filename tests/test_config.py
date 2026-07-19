import os
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app.config import (
    DEFAULT_SECRET,
    PLATFORM_AZURE,
    PLATFORM_LOCAL,
    Settings,
    load_settings,
)


def make_settings(**overrides):
    base = dict(
        environment="development",
        platform=PLATFORM_LOCAL,
        secret=DEFAULT_SECRET,
        database_url="sqlite:///test.db",
        data_dir=Path("test-data"),
        public_url=None,
        allowed_origins=frozenset(),
        cookie_secure=False,
        cookie_samesite="lax",
        host="127.0.0.1",
        port=8077,
        forwarded_allow_ips="127.0.0.1",
        registration_enabled=True,
        bootstrap_token_hash=None,
        db_size_warn_mb=256.0,
        db_size_alert_mb=1024.0,
        disk_free_warn_mb=1024.0,
        disk_free_alert_mb=256.0,
    )
    base.update(overrides)
    return Settings(**base)


# A minimal valid production environment used to isolate individual checks.
PROD_ENV = {
    "DEEPBOX_ENV": "production",
    "DEEPBOX_SECRET": "a-long-production-secret",
    "DEEPBOX_ALLOWED_ORIGINS": "https://deepbox.example.ts.net",
    "DEEPBOX_COOKIE_SECURE": "true",
    "DEEPBOX_PLATFORM": "local",
    "DEEPBOX_HOST": "127.0.0.1",
    "DEEPBOX_PORT": "8077",
    "DEEPBOX_FORWARDED_ALLOW_IPS": "127.0.0.1",
}


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEEPBOX_")}
    env.pop("PORT", None)
    env.pop("WEBSITES_PORT", None)
    if extra:
        env.update(extra)
    return env


class SettingsTests(unittest.TestCase):
    def test_development_allows_local_origin_without_allowlist(self):
        settings = make_settings()
        self.assertTrue(settings.origin_allowed("http://localhost:8077"))
        self.assertTrue(settings.origin_allowed(None))

    def test_production_requires_secret_origin_and_secure_cookie(self):
        settings = make_settings(environment="production")
        with self.assertRaisesRegex(RuntimeError, "DEEPBOX_SECRET"):
            settings.validate()

    def test_public_url_becomes_allowed_origin(self):
        env = clean_env({
            "DEEPBOX_ENV": "production",
            "DEEPBOX_SECRET": "a-long-production-secret",
            "DEEPBOX_PUBLIC_URL": "https://deepbox.example.ts.net/",
            "DEEPBOX_COOKIE_SECURE": "true",
            "DEEPBOX_PORT": "8077",
        })
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.public_url, "https://deepbox.example.ts.net")
        self.assertTrue(settings.origin_allowed("https://deepbox.example.ts.net"))
        self.assertFalse(settings.origin_allowed("https://evil.example"))
        self.assertFalse(settings.origin_allowed(None))


class PlatformTests(unittest.TestCase):
    def test_invalid_platform_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "DEEPBOX_PLATFORM"):
            make_settings(platform="gcp").validate()

    def test_production_local_rejects_non_loopback_host(self):
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            make_settings(
                environment="production",
                secret="x-secret",
                allowed_origins=frozenset({"https://x.example"}),
                cookie_secure=True,
                host="0.0.0.0",
            ).validate()

    def test_production_azure_allows_all_interfaces(self):
        # Should not raise: azure-app-service is the sanctioned 0.0.0.0 case.
        make_settings(
            environment="production",
            platform=PLATFORM_AZURE,
            secret="x-secret",
            allowed_origins=frozenset({"https://x.example"}),
            cookie_secure=True,
            host="0.0.0.0",
        ).validate()

    def test_azure_defaults_forwarded_allow_ips_wildcard(self):
        env = clean_env(dict(PROD_ENV, **{
            "DEEPBOX_PLATFORM": "azure-app-service",
            "DEEPBOX_HOST": "0.0.0.0",
        }))
        env.pop("DEEPBOX_FORWARDED_ALLOW_IPS", None)
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.forwarded_allow_ips, "*")
        self.assertTrue(settings.is_azure)


class PortTests(unittest.TestCase):
    def test_websites_port_used_when_deepbox_port_absent(self):
        env = clean_env(dict(PROD_ENV, **{"WEBSITES_PORT": "8000"}))
        env.pop("DEEPBOX_PORT", None)
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.port, 8000)

    def test_deepbox_port_takes_priority(self):
        env = clean_env(dict(PROD_ENV, **{
            "PORT": "9000",
            "DEEPBOX_PORT": "8077",
        }))
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.port, 8077)


class RegistrationTests(unittest.TestCase):
    def test_registration_disabled_by_default_in_production(self):
        with patch.dict(os.environ, clean_env(PROD_ENV), clear=True):
            settings = load_settings()
        self.assertFalse(settings.registration_enabled)

    def test_registration_enabled_by_default_in_development(self):
        with patch.dict(os.environ, clean_env(), clear=True):
            settings = load_settings()
        self.assertTrue(settings.registration_enabled)

    def test_registration_can_be_explicitly_enabled_in_production(self):
        env = clean_env(dict(PROD_ENV, **{"DEEPBOX_REGISTRATION_ENABLED": "true"}))
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertTrue(settings.registration_enabled)


class BootstrapTokenConfigTests(unittest.TestCase):
    def test_no_bootstrap_token_means_none(self):
        with patch.dict(os.environ, clean_env(), clear=True):
            settings = load_settings()
        self.assertIsNone(settings.bootstrap_token_hash)

    def test_only_hash_retained_and_plaintext_cleared(self):
        import hashlib
        raw = "super-secret-bootstrap-token"
        env = clean_env({"DEEPBOX_BOOTSTRAP_TOKEN": raw})
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
            # Plaintext env var must be cleared during load.
            self.assertNotIn("DEEPBOX_BOOTSTRAP_TOKEN", os.environ)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        self.assertEqual(settings.bootstrap_token_hash, expected)
        # Settings must never hold the plaintext anywhere.
        self.assertNotIn(raw, repr(settings))


if __name__ == "__main__":
    unittest.main()
