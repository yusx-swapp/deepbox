"""Command dispatcher for the user-installed ``deepbox`` launcher."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from . import client


HELP = """usage: deepbox <command> [options]

Commands:
  connect    Connect this machine to a deepbox server
  doctor     Check server reachability and connector credentials
  status     Show local connector status
  project    Manage connector-local project paths
  skill      Install and manage connector-local Agent Skills
  upgrade    Refresh the installed connector explicitly

Run 'deepbox <command> --help' for connector command options.
"""


class CommandError(ValueError):
    """Raised when a top-level deepbox command is not recognized."""


def connector_argv(argv: Sequence[str]) -> list[str] | None:
    """Translate a deepbox command into the existing connector argument shape.

    ``None`` means that top-level help should be printed instead of starting the
    connector. Leading options remain supported for compatibility with the old
    ``deepbox-connect`` launcher.
    """

    args = list(argv)
    if not args or args[0] in {"help", "-h", "--help"}:
        return None

    command, rest = args[0], args[1:]
    if command == "connect":
        return rest
    if command == "doctor":
        return ["--doctor", *rest]
    if command == "status":
        return ["--status", *rest]
    if command in {"project", "skill"}:
        return args
    if command == "upgrade":
        raise CommandError(
            "upgrade must be run through the installed deepbox command"
        )
    if command.startswith("-"):
        return args
    raise CommandError(f"unknown command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        routed = connector_argv(args)
    except CommandError as exc:
        print(f"deepbox: {exc}", file=sys.stderr)
        print(HELP, file=sys.stderr, end="")
        return 2

    if routed is None:
        print(HELP, end="")
        return 0

    asyncio.run(client.main(routed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
