"""Unit tests for the connector runtime adapter registry (planning.md Cut 7).

These tests are connector-only and require no real CLI to be installed.
"""
from __future__ import annotations

import sys

import pytest

from connector import runtimes
from connector.pty_session import resolve_cmd


# ---------------------------------------------------------------------------
# Registry: uniqueness and lookup
# ---------------------------------------------------------------------------

def test_registry_ids_are_unique():
    ids = runtimes.runtime_ids()
    assert len(ids) == len(set(ids)), "runtime ids must be unique"


def test_expected_runtimes_registered():
    for rid in ("mock", "claude-code", "copilot-cli", "codex-cli"):
        assert runtimes.has(rid)
        assert runtimes.get(rid).id == rid


def test_surface_lookup_accepts_family_and_legacy_adapter_ids():
    assert runtimes.get_for_surface("claude-code", "structured").id == "claude-code-structured"
    assert runtimes.get_for_surface("claude-code", "terminal").id == "claude-code"
    assert runtimes.get_for_surface("claude-code-structured", "terminal").id == "claude-code"


def test_surface_lookup_never_falls_back_to_another_surface():
    with pytest.raises(runtimes.UnknownRuntimeError):
        runtimes.get_for_surface("codex-cli", "structured")


def test_adapter_declares_personal_and_project_skill_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    claude = runtimes.get("claude-code")
    personal = {path.replace("\\", "/") for path in claude.skill_roots()}
    project = {path.replace("\\", "/") for path in claude.skill_roots(str(tmp_path / "repo"))}
    assert any(path.endswith("/.claude/skills") for path in personal)
    assert any(path.endswith("/.agents/skills") for path in personal)
    assert any(path.endswith("/.claude/skills") for path in project)
    assert any(path.endswith("/.agents/skills") for path in project)
    assert claude.capabilities(installed=True)["features"]["skills"] is True
    assert runtimes.get("mock").skill_roots() == ()


def test_register_rejects_duplicate():
    existing = runtimes.get("claude-code")
    with pytest.raises(ValueError):
        runtimes.register(runtimes.RuntimeAdapter(
            id="claude-code", label="dup", base_argv=("claude",)))
    # Original untouched.
    assert runtimes.get("claude-code") is existing


def test_get_unknown_raises_unknown_runtime():
    with pytest.raises(runtimes.UnknownRuntimeError):
        runtimes.get("does-not-exist")


def test_build_command_unknown_runtime_fails():
    with pytest.raises(runtimes.UnknownRuntimeError):
        runtimes.build_command("nope")


# ---------------------------------------------------------------------------
# Exact command argv per runtime / model / permission mode
# ---------------------------------------------------------------------------

def test_mock_base_command_uses_current_interpreter():
    assert runtimes.build_command("mock") == [
        sys.executable, "-u", "-m", "connector.mockcli"]


def test_claude_default_is_base_argv():
    # No model / permission -> exactly the historical base command.
    assert runtimes.build_command("claude-code") == ["claude"]


def test_claude_model_and_permission_argv():
    assert runtimes.build_command(
        "claude-code", model="opus", permission_mode="plan") == [
        "claude", "--model", "opus", "--permission-mode", "plan"]


def test_claude_bypass_permissions_argv():
    assert runtimes.build_command(
        "claude-code", permission_mode="bypassPermissions") == [
        "claude", "--dangerously-skip-permissions"]


def test_copilot_model_and_allow_all_argv():
    assert runtimes.build_command(
        "copilot-cli", model="gpt-5", permission_mode="allowAll") == [
        "copilot", "--model", "gpt-5", "--allow-all-tools"]


def test_codex_full_auto_argv():
    assert runtimes.build_command(
        "codex-cli", model="gpt-5-codex", permission_mode="full-auto") == [
        "codex", "--model", "gpt-5-codex",
        "--ask-for-approval", "never", "--sandbox", "workspace-write"]


def test_codex_default_permission_argv():
    assert runtimes.build_command("codex-cli", permission_mode="default") == [
        "codex", "--ask-for-approval", "on-request"]


def test_custom_model_is_allowed_but_unsafe_model_is_rejected():
    assert runtimes.build_command(
        "claude-code", model="new-provider-model")[-2:] == [
            "--model", "new-provider-model"]
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.build_command("claude-code", model="unsafe\nmodel")


def test_unsupported_permission_mode_rejected():
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.build_command("claude-code", permission_mode="fake-mode")


# ---------------------------------------------------------------------------
# Security: executable / argv validation, no shell metacharacters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "claude; rm -rf /", "cla ude", "../bin/claude", "a|b", "$(x)", "a&b", "",
])
def test_validate_executable_rejects_bad_names(bad):
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.validate_executable(bad)


def test_validate_program_allows_paths_but_blocks_metachars():
    # Absolute interpreter path is fine.
    assert runtimes.validate_program(sys.executable) == sys.executable
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.validate_program("/bin/sh; echo hi")


@pytest.mark.parametrize("tok", ["a;b", "a|b", "`x`", "$(x)", "a\nb", ""])
def test_validate_argv_rejects_bad_tokens(tok):
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.validate_argv(["claude", tok])


def test_register_rejects_pathy_executable():
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.register(runtimes.RuntimeAdapter(
            id="pathy", label="x", base_argv=("/bin/sh",)))


# ---------------------------------------------------------------------------
# Adding an adapter is localized (no edits to builder/other adapters needed)
# ---------------------------------------------------------------------------

def test_adding_adapter_is_localized():
    before = set(runtimes.runtime_ids())
    assert "temp-runtime" not in before
    new = runtimes.RuntimeAdapter(
        id="temp-runtime",
        label="Temp",
        base_argv=("mytool",),
        model_flag="-m",
        models=("x1",),
        permission_modes={"": (), "safe": ("--safe",)},
    )
    try:
        runtimes.register(new)
        # The *shared* builder handles it with zero changes.
        assert runtimes.build_command("temp-runtime") == ["mytool"]
        assert runtimes.build_command(
            "temp-runtime", model="x1", permission_mode="safe") == [
            "mytool", "-m", "x1", "--safe"]
        # Every other adapter's output is unchanged.
        assert runtimes.build_command("claude-code") == ["claude"]
    finally:
        runtimes._REGISTRY.pop("temp-runtime", None)
    assert set(runtimes.runtime_ids()) == before


# ---------------------------------------------------------------------------
# resolve_cmd integration preserves CLI behavior
# ---------------------------------------------------------------------------

def test_resolve_cmd_defaults_preserved():
    assert resolve_cmd("mock", None) == [
        sys.executable, "-u", "-m", "connector.mockcli"]
    assert resolve_cmd("claude-code", None) == ["claude"]


def test_resolve_cmd_unknown_runtime_falls_back_to_mock():
    assert resolve_cmd("bogus", None) == [
        sys.executable, "-u", "-m", "connector.mockcli"]


def test_resolve_cmd_explicit_launch_cmd_wins_and_is_validated():
    assert resolve_cmd("claude-code", "claude --model opus") == [
        "claude", "--model", "opus"]
    with pytest.raises(runtimes.InvalidCommandError):
        resolve_cmd("claude-code", "claude; rm -rf /")


def test_resolve_cmd_passes_model_and_permission():
    assert resolve_cmd("codex-cli", None, model="o4-mini",
                       permission_mode="auto") == [
        "codex", "--model", "o4-mini", "--ask-for-approval", "on-failure"]


def test_capabilities_blob_has_no_secrets():
    caps = runtimes.get("claude-code").capabilities(installed=True, version="1.2.3")
    assert caps["runtime"] == "claude-code"
    assert caps["installed"] is True
    assert "features" in caps
    # Sanity: nothing that looks like a token/secret key.
    text = repr(caps).lower()
    assert "token" not in text and "secret" not in text and "password" not in text


class TestStructuredControls:
    def test_capabilities_publish_generic_controls_without_local_path(self):
        cap = runtimes.get("claude-code-structured").capabilities(
            installed=True, version="1.2.3", path="C:/private/claude.exe")
        assert "path" not in cap
        controls = cap["features"]["controls"]
        assert [c["key"] for c in controls] == [
            "model", "reasoning_effort", "attachments"]
        assert controls[0]["scope"] == "session"
        assert controls[2]["kind"] == "file"

    def test_copilot_uses_documented_reasoning_choices_and_nonblocking_auth(self):
        adapter = runtimes.get("copilot-cli-structured")
        assert adapter.auth_argv == ()
        reasoning = next(
            control for control in adapter.controls
            if control.key == "reasoning_effort")
        assert reasoning.choices == ("low", "medium", "high", "xhigh", "max")
        assert runtimes.get("copilot-cli").auth_argv == ()
        assert adapter.install_url == (
            "https://docs.github.com/en/copilot/how-tos/copilot-cli/"
            "set-up-copilot-cli/install-copilot-cli")

    def test_sanitize_and_argv_ignore_undeclared_or_invalid_options(self):
        clean = runtimes.sanitize_options("copilot-cli-structured", {
            "model": "gpt-5",
            "reasoning_effort": "high",
            "evil": "--run-anything",
            "attachments": [{"name": "a.txt", "data": "YQ=="}],
        })
        assert "evil" not in clean
        assert runtimes.control_argv(
            "copilot-cli-structured", clean, ("C:/tmp/a.txt",)) == [
                "--reasoning-effort", "high", "--attachment", "C:/tmp/a.txt"]
        assert runtimes.sanitize_options("copilot-cli-structured", {
            "model": "new-provider-model", "reasoning_effort": "ultra"}) == {
                "model": "new-provider-model"}

    def test_permission_mode_is_sanitized_for_structured_turns(self):
        assert runtimes.sanitize_options(
            "claude-code-structured", {"permission_mode": "plan"}) == {
                "permission_mode": "plan"}
        assert runtimes.sanitize_options(
            "claude-code-structured",
            {"permission_mode": "not-a-mode"}) == {}

    def test_claude_file_control_uses_prompt_transport(self):
        control = runtimes.attachment_control("claude-code-structured")
        assert control is not None
        assert control.flag is None
        assert control.max_total_bytes == 1024 * 1024
