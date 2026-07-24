from __future__ import annotations

import asyncio
import json

import pytest

from connector import cli


def test_connector_argv_routes_user_facing_commands():
    assert cli.connector_argv(["connect", "--mode", "transport"]) == [
        "--mode",
        "transport",
    ]
    assert cli.connector_argv(["doctor", "--server-url", "https://box.test"]) == [
        "--doctor",
        "--server-url",
        "https://box.test",
    ]
    assert cli.connector_argv(["status"]) == ["--status"]
    assert cli.connector_argv(["project", "list"]) == ["project", "list"]
    assert cli.connector_argv(["skill", "list"]) == ["skill", "list"]


def test_connector_argv_keeps_legacy_options_and_handles_help():
    assert cli.connector_argv(["--doctor"]) == ["--doctor"]
    assert cli.connector_argv([]) is None
    assert cli.connector_argv(["--help"]) is None


def test_connector_argv_rejects_unknown_or_unwrapped_upgrade():
    with pytest.raises(cli.CommandError, match="unknown command: remove"):
        cli.connector_argv(["remove"])
    with pytest.raises(cli.CommandError, match="installed deepbox command"):
        cli.connector_argv(["upgrade"])


def test_main_prints_help_without_starting_connector(capsys):
    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert "usage: deepbox <command>" in captured.out
    assert captured.err == ""


def test_main_reports_unknown_command(capsys):
    assert cli.main(["wat"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown command: wat" in captured.err
    assert "usage: deepbox <command>" in captured.err


def test_main_runs_client_with_translated_argv(monkeypatch):
    seen = []

    async def fake_connector_main(argv):
        seen.append(argv)

    monkeypatch.setattr(cli.client, "main", fake_connector_main)

    assert cli.main(["connect", "--mode", "transport"]) == 0
    assert seen == [["--mode", "transport"]]


def test_main_preserves_connector_system_exit(monkeypatch):
    async def fake_connector_main(argv):
        raise SystemExit(7)

    monkeypatch.setattr(cli.client, "main", fake_connector_main)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["doctor"])
    assert exc_info.value.code == 7


def test_skill_commands_install_list_inspect_and_remove_locally(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    source = tmp_path / "review-code"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: review-code\ndescription: Review code safely.\n---\n# Review\n",
        encoding="utf-8")
    state = tmp_path / "state.db"

    asyncio.run(cli.client.main([
        "--state-path", str(state), "skill", "install", str(source)]))
    installed = json.loads(capsys.readouterr().out)
    assert installed["name"] == "review-code"
    assert installed["status"] == "installed"
    assert set(installed["targets"]) == {
        "claude-code", "copilot-cli", "codex-cli"}
    assert "store_path" not in installed
    assert (home / ".claude" / "skills" / "review-code" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "review-code" / "SKILL.md").is_file()

    asyncio.run(cli.client.main([
        "--state-path", str(state), "skill", "list"]))
    listed = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in listed] == ["review-code"]

    asyncio.run(cli.client.main([
        "--state-path", str(state), "skill", "inspect", "review-code"]))
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["digest"] == installed["digest"]
    assert "bindings" not in inspected

    asyncio.run(cli.client.main([
        "--state-path", str(state), "skill", "remove", "review-code"]))
    assert "skill removed: review-code" in capsys.readouterr().out
    assert not (home / ".claude" / "skills" / "review-code").exists()
    assert not (home / ".agents" / "skills" / "review-code").exists()
