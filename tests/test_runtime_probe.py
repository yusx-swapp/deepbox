from __future__ import annotations

import json

import pytest

from connector import runtimes
from connector.runtime_probe import (
    ProbeResult,
    RuntimeProbeCache,
    _with_revision,
    availability,
    probe_family,
)


def _adapter(**overrides):
    values = {
        "id": "test-structured",
        "label": "Test CLI",
        "base_argv": ("test-cli",),
        "family": "test-cli",
        "surface": "structured",
        "default_surface": True,
        "structured": True,
        "models": ("fallback-model",),
        "install_url": "https://example.test/install",
        "install_command": "npm install -g test-cli",
        "auth_argv": ("auth", "status"),
    }
    values.update(overrides)
    return runtimes.RuntimeAdapter(**values)


def test_revision_ignores_probe_timestamps_but_tracks_capability_changes():
    first = _with_revision({
        "id": "test-cli",
        "probed_at": 1,
        "installation": {"status": "ok", "probed_at": 2},
        "models": {"items": ["one"], "probed_at": 3},
    })
    second = _with_revision({
        "id": "test-cli",
        "probed_at": 10,
        "installation": {"status": "ok", "probed_at": 20},
        "models": {"items": ["one"], "probed_at": 30},
    })
    changed = _with_revision({
        "id": "test-cli",
        "probed_at": 10,
        "installation": {"status": "missing", "probed_at": 20},
        "models": {"items": ["one"], "probed_at": 30},
    })

    assert first["revision"] == second["revision"]
    assert first["revision"] != changed["revision"]


def test_probe_missing_runtime_is_reported_without_host_paths(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(runtimes, "all_adapters", lambda: [adapter])
    monkeypatch.setattr("connector.runtime_probe.shutil.which", lambda _: None)

    capability = probe_family("test-cli")

    assert capability["schema_version"] == 2
    assert capability["runtime"] == "test-cli"
    assert capability["installation"] == {
        "status": "missing",
        "version": None,
        "guidance": {
            "url": "https://example.test/install",
            "command": "npm install -g test-cli",
        },
    }
    assert capability["compatibility"] == {"status": "not_applicable"}
    assert capability["authentication"] == {"status": "not_applicable"}
    surface = capability["surfaces"][0]
    assert surface["id"] == "structured"
    assert surface["available"] is False
    assert surface["default"] is True
    assert surface["legacy_runtime_id"] == "test-structured"
    assert surface["features"] == {
        "models": ["fallback-model"],
        "permission_modes": [],
        "structured": True,
        "per_turn": False,
        "skills": False,
        "controls": [{
            "key": "model",
            "label": "Model",
            "kind": "select",
            "scope": "session",
            "choices": ["fallback-model"],
            "allow_custom": True,
        }],
    }
    assert capability["models"]["status"] == "unknown"
    assert "executable" not in json.dumps(capability)
    assert "path" not in json.dumps(capability)
    assert len(capability["revision"]) == 16


def test_probe_discovers_models_and_normalizes_auth_and_version(monkeypatch):
    adapter = _adapter(
        probe_hint=lambda: True,
        model_discovery_argv=("models", "--json"),
        model_discovery_parser=lambda output: tuple(json.loads(output)),
    )
    monkeypatch.setattr(runtimes, "all_adapters", lambda: [adapter])
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        if argv[-1] == "--version":
            return ProbeResult(0, "Test CLI 2.4\nSECRET=must-not-leak")
        if argv[-2:] == ["auth", "status"]:
            return ProbeResult(0, "token=must-not-leak")
        if argv[-2:] == ["models", "--json"]:
            return ProbeResult(0, '["alpha", "beta", "alpha"]')
        raise AssertionError(argv)

    capability = probe_family("test-cli", runner=runner)

    assert capability["installation"]["status"] == "installed"
    assert capability["installation"]["version"] == "Test CLI 2.4"
    assert capability["compatibility"]["status"] == "compatible"
    assert capability["authentication"]["status"] == "authenticated"
    assert capability["models"]["status"] == "complete"
    assert capability["models"]["source"] == "runtime"
    assert capability["models"]["items"] == [
        {"id": "alpha", "label": "alpha"},
        {"id": "beta", "label": "beta"},
    ]
    surface = capability["surfaces"][0]
    assert surface["features"]["models"] == ["alpha", "beta"]
    model_control = next(
        item for item in surface["features"]["controls"] if item["key"] == "model")
    assert model_control["choices"] == ["alpha", "beta"]
    serialized = json.dumps(capability)
    assert "must-not-leak" not in serialized
    assert all(timeout == 5.0 for _, timeout in calls)


def test_probe_falls_back_to_partial_adapter_models(monkeypatch):
    adapter = _adapter(probe_hint=lambda: True, auth_argv=())
    monkeypatch.setattr(runtimes, "all_adapters", lambda: [adapter])

    def runner(argv, timeout):
        return ProbeResult(0, "v1")

    capability = probe_family("test-cli", runner=runner)
    assert capability["authentication"] == {"status": "unknown"}
    assert capability["models"]["status"] == "partial"
    assert capability["models"]["source"] == "adapter"
    assert capability["models"]["items"] == [
        {"id": "fallback-model", "label": "fallback-model"},
    ]


def test_phase_a_probe_keeps_static_models_until_discovery_runs(monkeypatch):
    adapter = _adapter(probe_hint=lambda: True, auth_argv=())
    monkeypatch.setattr(runtimes, "all_adapters", lambda: [adapter])

    capability = probe_family(
        "test-cli",
        include_models=False,
        runner=lambda argv, timeout: ProbeResult(0, "v1"),
    )

    assert capability["models"]["status"] == "partial"
    assert capability["models"]["source"] == "adapter"
    assert capability["models"]["items"] == [
        {"id": "fallback-model", "label": "fallback-model"},
    ]
    model_control = next(
        item for item in capability["surfaces"][0]["features"]["controls"]
        if item["key"] == "model"
    )
    assert model_control["choices"] == ["fallback-model"]


def test_runtime_probe_cache_is_scoped_by_devbox_and_ttl(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(runtimes, "runtime_families", lambda: ["one"])

    def fake_probe(family):
        calls.append(family)
        return {"runtime": family, "call": len(calls)}

    monkeypatch.setattr("connector.runtime_probe.probe_family", fake_probe)
    cache = RuntimeProbeCache(ttl_seconds=10, clock=lambda: now[0])

    assert cache.probe_all("box-a")[0]["call"] == 1
    assert cache.probe_all("box-a")[0]["call"] == 1
    assert cache.probe_all("box-b")[0]["call"] == 2
    now[0] = 111
    assert cache.probe_all("box-a")[0]["call"] == 3
    assert cache.probe_all("box-a", force=True)[0]["call"] == 4


def test_availability_keeps_states_orthogonal_and_never_falls_back():
    capability = {
        "installation": {"status": "installed"},
        "compatibility": {"status": "compatible"},
        "authentication": {"status": "authenticated"},
        "surfaces": [
            {"id": "terminal", "available": True},
            {"id": "structured", "available": False},
        ],
    }
    assert availability(capability, "terminal") == (True, "available")
    assert availability(capability, "structured") == (False, "surface_unavailable")
    assert availability(capability, "unknown") == (False, "surface_unavailable")

    capability["authentication"]["status"] = "unauthenticated"
    assert availability(capability, "terminal") == (
        False, "runtime_not_authenticated")


def test_family_surface_resolution_is_explicit():
    assert runtimes.get_for_surface("claude-code", "structured").id == (
        "claude-code-structured")
    assert runtimes.get_for_surface("claude-code", "terminal").id == "claude-code"
    with pytest.raises(runtimes.UnknownRuntimeError, match="no 'structured' surface"):
        runtimes.get_for_surface("codex-cli", "structured")
