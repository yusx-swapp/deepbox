"""Safe, connector-local runtime capability probing.

The server receives only normalized states and public metadata. Executable paths,
raw command output, environment variables, and CLI credentials never leave the
connector process.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import runtimes

CAPABILITY_SCHEMA_VERSION = 2
DEFAULT_PROBE_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class ProbeResult:
    returncode: int | None
    stdout: str = ""
    timed_out: bool = False
    error: bool = False


def run_probe(argv: list[str], timeout: float = 5.0) -> ProbeResult:
    """Run one declared argv probe without a shell and with bounded output."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
        return ProbeResult(completed.returncode, completed.stdout[:64 * 1024])
    except subprocess.TimeoutExpired:
        return ProbeResult(None, timed_out=True)
    except (OSError, ValueError):
        return ProbeResult(None, error=True)


def _safe_version(raw: str) -> str | None:
    for line in raw.splitlines():
        clean = "".join(ch for ch in line.strip() if ord(ch) >= 0x20)
        if clean:
            return clean[:160]
    return None


def _probe_command(adapter: runtimes.RuntimeAdapter, suffix: tuple[str, ...],
                   runner: Callable[[list[str], float], ProbeResult]) -> ProbeResult:
    return runner([adapter.executable, *suffix], 5.0)


def _surface_json(adapter: runtimes.RuntimeAdapter, available: bool) -> dict:
    # Reuse the adapter's generic feature builder so synthesized model and
    # permission controls remain available during the v1 -> v2 migration.
    features = adapter.capabilities(installed=available)["features"]
    return {
        "id": adapter.surface_id,
        "available": available,
        "default": adapter.default_surface,
        "legacy_runtime_id": adapter.id,
        "features": features,
    }


def _with_revision(capability: dict) -> dict:
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in {"revision", "probed_at"}}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    payload = json.dumps(stable(capability), sort_keys=True, separators=(",", ":"))
    result = dict(capability)
    result["revision"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return result


def probe_family(
    family: str,
    *,
    runner: Callable[[list[str], float], ProbeResult] = run_probe,
    include_models: bool = True,
) -> dict:
    """Probe one runtime family and return the normalized capability-v2 blob."""
    adapters = [adapter for adapter in runtimes.all_adapters()
                if adapter.family_id == family]
    if not adapters:
        raise runtimes.UnknownRuntimeError(f"unknown runtime family {family!r}")
    representative = next((item for item in adapters if item.default_surface), adapters[0])

    hinted = representative.probe_hint() if representative.probe_hint else None
    installed = bool(hinted) if hinted is not None else bool(shutil.which(representative.executable))
    version = None
    version_result = None
    if installed and representative.version_argv:
        version_result = _probe_command(representative, representative.version_argv, runner)
        if version_result.returncode == 0:
            version = _safe_version(version_result.stdout)

    installation = {
        "status": "installed" if installed else "missing",
        "version": version,
        "guidance": {
            "url": representative.install_url,
            "command": representative.install_command,
        },
    }
    if not installed:
        compatibility = {"status": "not_applicable"}
    elif not representative.version_argv or (version_result and version_result.returncode == 0):
        compatibility = {"status": "compatible"}
    else:
        compatibility = {"status": "unknown", "reason": "version_probe_failed"}

    if not installed:
        authentication = {"status": "not_applicable"}
    elif not representative.auth_argv:
        authentication = {"status": "unknown"}
    else:
        auth_result = _probe_command(representative, representative.auth_argv, runner)
        if auth_result.returncode == 0:
            authentication = {"status": "authenticated"}
        elif auth_result.returncode is not None:
            authentication = {
                "status": "unauthenticated",
                "reason": "cli_reported_logged_out",
            }
        else:
            authentication = {
                "status": "error" if auth_result.error else "unknown",
                "reason": "auth_probe_failed",
            }

    probed_at = datetime.now(timezone.utc).isoformat()
    model_ids: list[str] = []
    model_status = "unknown"
    model_source = "none"
    if installed and include_models and representative.model_discovery_argv:
        discovery = _probe_command(
            representative, representative.model_discovery_argv, runner)
        if discovery.returncode == 0 and representative.model_discovery_parser:
            try:
                model_ids = list(dict.fromkeys(
                    value for value in representative.model_discovery_parser(discovery.stdout)
                    if isinstance(value, str) and value.strip()))
            except (TypeError, ValueError):
                model_ids = []
            if model_ids:
                model_status = "complete"
                model_source = "runtime"
    if installed and not model_ids:
        model_ids = list(dict.fromkeys(
            model for adapter in adapters for model in adapter.models))
        if model_ids:
            model_status = "partial"
            model_source = "adapter"
    available = installed and compatibility["status"] != "incompatible"
    surfaces = [_surface_json(adapter, available) for adapter in adapters]
    for adapter, surface_item in zip(adapters, surfaces):
        if model_ids:
            surface_item["features"]["models"] = list(model_ids)
        for control in surface_item["features"].get("controls", []):
            if control.get("key") == "model":
                if model_ids:
                    control["choices"] = list(model_ids)
                control["allow_custom"] = adapter.allow_custom_models
    # Exactly one intended default per family. Prefer an explicitly-declared
    # default, then structured, then registry order.
    defaults = [item for item in surfaces if item["default"]]
    if len(defaults) != 1:
        preferred = next((item for item in surfaces if item["id"] == "structured"), surfaces[0])
        for item in surfaces:
            item["default"] = item is preferred

    capability = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "runtime": family,
        "label": representative.label,
        "legacy_runtime_ids": [adapter.id for adapter in adapters],
        "installation": installation,
        "compatibility": compatibility,
        "authentication": authentication,
        "surfaces": surfaces,
        "models": {
            "status": model_status,
            "source": model_source,
            "items": [{"id": model, "label": model} for model in model_ids],
            "default": representative.default_model,
            "allow_custom": any(adapter.allow_custom_models for adapter in adapters),
            "probed_at": probed_at,
        },
    }
    return _with_revision(capability)


def availability(capability: dict, surface: str) -> tuple[bool, str]:
    """Reduce orthogonal capability states to one spawn-time decision."""
    installation = capability.get("installation", {}).get("status")
    if installation != "installed":
        return False, "runtime_not_installed"
    compatibility = capability.get("compatibility", {}).get("status")
    if compatibility == "incompatible":
        return False, "runtime_incompatible"
    authentication = capability.get("authentication", {}).get("status")
    if authentication == "unauthenticated":
        return False, "runtime_not_authenticated"
    if authentication == "error":
        return False, "runtime_auth_probe_failed"
    selected = next((item for item in capability.get("surfaces", [])
                     if item.get("id") == surface), None)
    if not selected or not selected.get("available"):
        return False, "surface_unavailable"
    return True, "available"


class RuntimeProbeCache:
    """TTL cache scoped by server devbox identity and runtime family."""

    def __init__(self, ttl_seconds: float = DEFAULT_PROBE_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._entries: dict[tuple[str, str], tuple[float, dict]] = {}

    def probe_all(self, devbox_id: str, *, force: bool = False) -> list[dict]:
        now = self.clock()
        capabilities = []
        for family in runtimes.runtime_families():
            key = (devbox_id, family)
            cached = self._entries.get(key)
            if force or cached is None or cached[0] <= now:
                value = probe_family(family)
                self._entries[key] = (now + self.ttl_seconds, value)
            else:
                value = cached[1]
            capabilities.append(value)
        return capabilities

    def invalidate(self, devbox_id: str | None = None) -> None:
        if devbox_id is None:
            self._entries.clear()
            return
        self._entries = {
            key: value for key, value in self._entries.items()
            if key[0] != devbox_id
        }
