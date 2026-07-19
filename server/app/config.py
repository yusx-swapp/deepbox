"""Environment-backed deepbox server configuration.

Development defaults keep local setup simple. Production mode deliberately
fails closed when the signing secret or browser origin allowlist is missing.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SECRET = "dev-secret-change-me"

# Supported deployment platforms. ``local`` covers laptop / Tailscale-fronted
# hosts (uvicorn stays loopback). ``azure-app-service`` is the only platform
# allowed to bind a non-loopback host in production, because there the
# platform's own front end terminates TLS and forwards to the container.
PLATFORM_LOCAL = "local"
PLATFORM_AZURE = "azure-app-service"
VALID_PLATFORMS = {PLATFORM_LOCAL, PLATFORM_AZURE}


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _origins(raw: str) -> frozenset[str]:
    return frozenset(
        value.strip().rstrip("/")
        for value in raw.split(",")
        if value.strip()
    )


@dataclass(frozen=True)
class Settings:
    environment: str
    platform: str
    secret: str
    database_url: str
    data_dir: Path
    public_url: str | None
    allowed_origins: frozenset[str]
    cookie_secure: bool
    cookie_samesite: str
    host: str
    port: int
    forwarded_allow_ips: str
    registration_enabled: bool
    bootstrap_token_hash: str | None
    # Capacity thresholds (operator-tunable). Warn is a soft signal; alert is
    # a hard signal an operator should act on. Database size is measured in MB;
    # recording-disk free space is measured in MB remaining.
    db_size_warn_mb: float
    db_size_alert_mb: float
    disk_free_warn_mb: float
    disk_free_alert_mb: float

    @property
    def production(self) -> bool:
        return self.environment == "production"

    @property
    def is_azure(self) -> bool:
        return self.platform == PLATFORM_AZURE

    def origin_allowed(self, origin: str | None) -> bool:
        # Development remains convenient for localhost. Production always has
        # a non-empty allowlist because validate() rejects an empty one.
        if not self.allowed_origins:
            return not self.production
        if not origin:
            return False
        return origin.rstrip("/") in self.allowed_origins

    def validate(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise RuntimeError("DEEPBOX_ENV must be development, test, or production")
        if self.platform not in VALID_PLATFORMS:
            raise RuntimeError(
                "DEEPBOX_PLATFORM must be local or azure-app-service"
            )
        if self.production and self.secret == DEFAULT_SECRET:
            raise RuntimeError("DEEPBOX_SECRET must be set in production")
        if self.production and not self.allowed_origins:
            raise RuntimeError("DEEPBOX_ALLOWED_ORIGINS must be set in production")
        if self.production and not self.cookie_secure:
            raise RuntimeError("DEEPBOX_COOKIE_SECURE must be true in production")
        if self.cookie_samesite not in {"lax", "strict", "none"}:
            raise RuntimeError("DEEPBOX_COOKIE_SAMESITE must be lax, strict, or none")
        # Loopback-only in production, except on Azure App Service where the
        # managed front end terminates TLS and forwards to the container's
        # published port. That platform must bind 0.0.0.0 to be reachable.
        if self.production and self.host not in {"127.0.0.1", "localhost", "::1"}:
            if not (self.is_azure and self.host in {"0.0.0.0", "::"}):
                raise RuntimeError(
                    "DEEPBOX_HOST must be loopback in production "
                    "(0.0.0.0 only allowed on azure-app-service)"
                )
        if self.production and any(not origin.startswith("https://") for origin in self.allowed_origins):
            raise RuntimeError("production origins must use HTTPS")
        if not (1 <= self.port <= 65535):
            raise RuntimeError("DEEPBOX_PORT must be between 1 and 65535")
        for name, value in (
            ("DEEPBOX_DB_SIZE_WARN_MB", self.db_size_warn_mb),
            ("DEEPBOX_DB_SIZE_ALERT_MB", self.db_size_alert_mb),
            ("DEEPBOX_DISK_FREE_WARN_MB", self.disk_free_warn_mb),
            ("DEEPBOX_DISK_FREE_ALERT_MB", self.disk_free_alert_mb),
        ):
            if value < 0:
                raise RuntimeError(f"{name} must be non-negative")
        # A database that must alert before it warns is a misconfiguration.
        if self.db_size_alert_mb < self.db_size_warn_mb:
            raise RuntimeError(
                "DEEPBOX_DB_SIZE_ALERT_MB must be >= DEEPBOX_DB_SIZE_WARN_MB"
            )
        # Free-disk thresholds count down: alert triggers at a lower free
        # figure than warn, so the alert bound must not exceed the warn bound.
        if self.disk_free_alert_mb > self.disk_free_warn_mb:
            raise RuntimeError(
                "DEEPBOX_DISK_FREE_ALERT_MB must be <= DEEPBOX_DISK_FREE_WARN_MB"
            )


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


def _port() -> int:
    # Azure App Service (and many PaaS hosts) inject the listening port via
    # PORT / WEBSITES_PORT. Prefer the explicit deepbox variable, then the
    # platform-provided ones, then the local default.
    for name in ("DEEPBOX_PORT", "PORT", "WEBSITES_PORT"):
        raw = os.getenv(name)
        if raw and raw.strip():
            return int(raw.strip())
    return 8077


def load_settings() -> Settings:
    public_url = os.getenv("DEEPBOX_PUBLIC_URL", "").strip().rstrip("/") or None
    allowed = _origins(os.getenv("DEEPBOX_ALLOWED_ORIGINS", ""))
    # A configured public URL is also an allowed browser origin unless the
    # operator explicitly supplies additional origins.
    if public_url:
        allowed = frozenset({*allowed, public_url})
    platform = os.getenv("DEEPBOX_PLATFORM", PLATFORM_LOCAL).strip().lower()
    # forwarded_allow_ips defaults to loopback; on Azure the reverse proxy is
    # an internal, platform-managed hop so trusting it is required for correct
    # client IP / scheme handling. Operators can override explicitly.
    default_fwd = "*" if platform == PLATFORM_AZURE else "127.0.0.1"
    forwarded_allow_ips = os.getenv("DEEPBOX_FORWARDED_ALLOW_IPS", default_fwd).strip()
    environment = os.getenv("DEEPBOX_ENV", "development").strip().lower()
    # Registration is open by default for local development but fails closed
    # in production unless an operator explicitly enables it.
    registration_default = environment != "production"
    # The bootstrap token is used exactly once to create the first owner. We
    # never retain the plaintext: only its SHA-256 hash lives in Settings, and
    # the plaintext env var is cleared from the process environment.
    bootstrap_raw = os.getenv("DEEPBOX_BOOTSTRAP_TOKEN", "").strip()
    bootstrap_token_hash = (
        hashlib.sha256(bootstrap_raw.encode()).hexdigest() if bootstrap_raw else None
    )
    if "DEEPBOX_BOOTSTRAP_TOKEN" in os.environ:
        del os.environ["DEEPBOX_BOOTSTRAP_TOKEN"]
    result = Settings(
        environment=environment,
        platform=platform,
        secret=os.getenv("DEEPBOX_SECRET", DEFAULT_SECRET),
        database_url=os.getenv("DEEPBOX_DATABASE_URL", "sqlite:///deepbox.db"),
        data_dir=Path(os.getenv("DEEPBOX_DATA_DIR", str(PROJECT_DIR / "data"))).resolve(),
        public_url=public_url,
        allowed_origins=allowed,
        cookie_secure=_bool("DEEPBOX_COOKIE_SECURE", False),
        cookie_samesite=os.getenv("DEEPBOX_COOKIE_SAMESITE", "lax").strip().lower(),
        host=os.getenv("DEEPBOX_HOST", "127.0.0.1").strip(),
        port=_port(),
        forwarded_allow_ips=forwarded_allow_ips,
        registration_enabled=_bool("DEEPBOX_REGISTRATION_ENABLED", registration_default),
        bootstrap_token_hash=bootstrap_token_hash,
        db_size_warn_mb=_float("DEEPBOX_DB_SIZE_WARN_MB", 256.0),
        db_size_alert_mb=_float("DEEPBOX_DB_SIZE_ALERT_MB", 1024.0),
        disk_free_warn_mb=_float("DEEPBOX_DISK_FREE_WARN_MB", 1024.0),
        disk_free_alert_mb=_float("DEEPBOX_DISK_FREE_ALERT_MB", 256.0),
    )
    result.validate()
    return result


settings = load_settings()
