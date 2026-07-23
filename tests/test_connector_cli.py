from __future__ import annotations

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
