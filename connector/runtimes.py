"""Connector-side runtime adapter registry (planning.md Cut 7).

This module replaces the hard-coded ``DEFAULT_CMDS`` table in
:mod:`connector.pty_session` with a small, scalable registry of
:class:`RuntimeAdapter` objects. Each adapter carries the metadata needed to

  * probe whether the runtime is installed (``executable`` + :func:`probe`),
  * build the exact launch ``argv`` for a given model / permission mode via a
    single *shared* command builder (:func:`build_command`), and
  * describe its capabilities as an opaque JSON blob for the server.

Design goals (see planning.md Cut 7):

  * Adding a new runtime is *localized*: define one adapter and call
    :func:`register`. No edits to the builder, the supervisor, or other
    adapters are required.
  * The server and web UI never branch on runtime-specific logic; only the
    connector knows how to turn ``(runtime, model, permission_mode)`` into argv.

Security invariants enforced here:

  * Executable names are validated to be bare program names (no path
    separators, no shell metacharacters). We never pass a shell string; the
    caller spawns argv directly (``shell=False`` semantics).
  * Every argv token is validated to be a non-empty string free of shell
    metacharacters and control characters.
  * No secrets are embedded in argv, stored, or emitted; capability blobs only
    carry install/version/feature metadata.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "RuntimeAdapter",
    "UnknownRuntimeError",
    "InvalidCommandError",
    "register",
    "get",
    "has",
    "all_adapters",
    "runtime_ids",
    "build_command",
    "validate_executable",
    "validate_program",
    "validate_argv",
]


# Shell metacharacters that must never appear in an executable name or argv
# token. We spawn argv directly (no shell), but we still reject these to keep a
# defence-in-depth posture and to make injection attempts fail loudly.
_SHELL_METACHARS = set(";&|<>`$(){}[]!*?~\n\r\t\0\"'\\")
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


class UnknownRuntimeError(KeyError):
    """Raised when a runtime id has no registered adapter."""


class InvalidCommandError(ValueError):
    """Raised when a built argv or executable fails validation."""


def validate_executable(name: str) -> str:
    """Validate and return a bare executable name.

    Rejects anything containing a path separator or shell metacharacter so a
    runtime can never smuggle in ``/bin/sh -c`` style payloads. Used for the
    *declared* executables of registered adapters.
    """
    if not isinstance(name, str) or not name:
        raise InvalidCommandError("executable must be a non-empty string")
    if "/" in name or "\\" in name:
        raise InvalidCommandError(f"executable must be a bare name: {name!r}")
    if not _EXECUTABLE_RE.match(name):
        raise InvalidCommandError(f"invalid executable name: {name!r}")
    return name


def validate_program(program: str) -> str:
    """Validate argv[0] as a program to spawn.

    Accepts either a bare executable name or a concrete filesystem path (e.g.
    ``sys.executable``), but always rejects shell metacharacters and control
    characters. This is deliberately more permissive than
    :func:`validate_executable` so legitimate absolute interpreter paths work
    while injection payloads still fail.
    """
    if not isinstance(program, str) or not program:
        raise InvalidCommandError("program must be a non-empty string")
    # Path separators and a leading drive/colon are allowed; shell metacharacters
    # (minus the path chars) are not.
    forbidden = (_SHELL_METACHARS - {"\\"}) - {":"}
    bad = forbidden.intersection(program)
    if bad:
        raise InvalidCommandError(
            f"program {program!r} contains disallowed characters: {sorted(bad)!r}")
    if any(ord(ch) < 0x20 for ch in program):
        raise InvalidCommandError(f"program {program!r} contains control characters")
    return program


def validate_argv(argv: list[str]) -> list[str]:
    """Validate every token of a launch argv.

    The first token is validated as a program to spawn (bare name or path); the
    rest must be non-empty strings free of shell metacharacters and control
    characters.
    """
    if not argv:
        raise InvalidCommandError("argv must not be empty")
    validate_program(argv[0])
    for tok in argv[1:]:
        if not isinstance(tok, str) or tok == "":
            raise InvalidCommandError(f"argv token must be a non-empty string: {tok!r}")
        bad = _SHELL_METACHARS.intersection(tok)
        if bad:
            raise InvalidCommandError(
                f"argv token {tok!r} contains disallowed characters: {sorted(bad)!r}")
        if any(ord(ch) < 0x20 for ch in tok):
            raise InvalidCommandError(f"argv token {tok!r} contains control characters")
    return argv


@dataclass(frozen=True)
class RuntimeAdapter:
    """Metadata + command construction rules for a single runtime.

    Attributes:
        id: Stable runtime identifier (matches the server ``runtime`` field).
        label: Human-friendly name.
        base_argv: The base launch argv (executable + any always-on flags).
        model_flag: CLI flag used to select a model, or ``None`` if the runtime
            does not accept a model on the command line.
        models: Allowed model names. Empty means "any non-empty string".
        default_model: Model used when none is requested (may be ``None``).
        permission_modes: Maps a permission-mode name to the extra argv tokens
            that select it. The empty-string key (``""``) is the default mode.
        environment: Extra environment variables the runtime needs (no secrets).
        probe_hint: Optional callable overriding install detection (for mocks).
    """

    id: str
    label: str
    base_argv: tuple[str, ...]
    model_flag: str | None = None
    models: tuple[str, ...] = ()
    default_model: str | None = None
    permission_modes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    probe_hint: Callable[[], bool] | None = None
    # When True this runtime is driven headless via a structured JSON protocol
    # (connector.agent_session.StructuredAgentSession) instead of a PTY. The
    # web UI renders a chat surface (bubbles/tool cards/permission prompts)
    # rather than a terminal for such agents.
    structured: bool = False
    # Per-turn structured runtimes (e.g. Copilot ``-p``) spawn a fresh process
    # for each user turn instead of holding a persistent stdin session. When
    # True the connector uses StructuredAgentSession(per_turn=True) and appends
    # ``prompt_argv`` + the prompt text to argv for each turn.
    per_turn: bool = False
    prompt_argv: tuple[str, ...] = ()

    @property
    def executable(self) -> str:
        return self.base_argv[0]

    def capabilities(self, *, installed: bool, version: str | None = None,
                     path: str | None = None) -> dict:
        """Return an opaque JSON-serialisable capability blob for the server."""
        return {
            "runtime": self.id,
            "installed": installed,
            "version": version,
            "path": path,
            "features": {
                "models": list(self.models),
                "permission_modes": sorted(self.permission_modes),
                "structured": self.structured,
                "per_turn": self.per_turn,
            },
        }


# Ordered registry so probe/report order is deterministic.
_REGISTRY: dict[str, RuntimeAdapter] = {}


def register(adapter: RuntimeAdapter, *, replace: bool = False) -> RuntimeAdapter:
    """Register an adapter. Enforces id uniqueness and validates its base argv.

    Adding a runtime is a single call to this function; nothing else in the
    connector needs to change.
    """
    if not isinstance(adapter, RuntimeAdapter):
        raise TypeError("register() expects a RuntimeAdapter")
    if not adapter.id:
        raise InvalidCommandError("adapter id must be non-empty")
    if adapter.id in _REGISTRY and not replace:
        raise ValueError(f"duplicate runtime id: {adapter.id!r}")
    # Declared adapters must name a bare executable (strict); this blocks a
    # runtime from smuggling an absolute path or shell payload as argv[0].
    validate_executable(adapter.base_argv[0])
    validate_argv(list(adapter.base_argv))
    # Validate declared permission-mode flag tokens up front.
    for mode, extra in adapter.permission_modes.items():
        for tok in extra:
            if not isinstance(tok, str) or tok == "":
                raise InvalidCommandError(
                    f"permission mode {mode!r} has invalid token {tok!r}")
    _REGISTRY[adapter.id] = adapter
    return adapter


def get(runtime_id: str) -> RuntimeAdapter:
    try:
        return _REGISTRY[runtime_id]
    except KeyError:
        raise UnknownRuntimeError(
            f"unknown runtime {runtime_id!r}; known: {sorted(_REGISTRY)}") from None


def has(runtime_id: str) -> bool:
    return runtime_id in _REGISTRY


def all_adapters() -> list[RuntimeAdapter]:
    return list(_REGISTRY.values())


def runtime_ids() -> list[str]:
    return list(_REGISTRY)


def build_command(runtime_id: str, *, model: str | None = None,
                  permission_mode: str | None = None) -> list[str]:
    """Shared command builder: turn ``(runtime, model, permission_mode)``
    into a validated launch argv.

    With no ``model``/``permission_mode`` this returns the runtime's base argv,
    exactly preserving the historical ``DEFAULT_CMDS`` behaviour.

    Raises:
        UnknownRuntimeError: no adapter registered for ``runtime_id``.
        InvalidCommandError: the model or permission mode is not supported, or
            the resulting argv fails validation.
    """
    adapter = get(runtime_id)
    argv: list[str] = list(adapter.base_argv)

    # -- model selection ---------------------------------------------------
    chosen_model = model if model is not None else adapter.default_model
    if chosen_model is not None:
        if adapter.model_flag is None:
            raise InvalidCommandError(
                f"runtime {runtime_id!r} does not accept a model")
        if adapter.models and chosen_model not in adapter.models:
            raise InvalidCommandError(
                f"runtime {runtime_id!r} does not support model {chosen_model!r}; "
                f"allowed: {list(adapter.models)}")
        argv += [adapter.model_flag, chosen_model]

    # -- permission mode ---------------------------------------------------
    if permission_mode is None:
        mode_key = "" if "" in adapter.permission_modes else None
    else:
        mode_key = permission_mode
    if mode_key is not None:
        if mode_key not in adapter.permission_modes:
            raise InvalidCommandError(
                f"runtime {runtime_id!r} does not support permission mode "
                f"{permission_mode!r}; allowed: {sorted(adapter.permission_modes)}")
        argv += list(adapter.permission_modes[mode_key])

    return validate_argv(argv)


# ---------------------------------------------------------------------------
# First-batch adapters (planning.md Cut 7): mock + claude / copilot / codex.
# Each of these is intentionally self-contained: it is exactly the "one adapter
# file entry + one registry entry" unit the acceptance criteria call for.
# ---------------------------------------------------------------------------

# The mock runtime launches the *current* interpreter; its argv[0] is an
# absolute path (sys.executable), which is legitimately not a bare name, so it
# is registered directly without the bare-name executable check that
# :func:`register` would apply. All other adapters go through :func:`register`.
_REGISTRY["mock"] = RuntimeAdapter(
    id="mock",
    label="Mock Agent",
    base_argv=(sys.executable, "-u", "-m", "connector.mockcli"),
    probe_hint=lambda: True,
)

register(RuntimeAdapter(
    id="claude-code",
    label="Claude Code",
    base_argv=("claude",),
    model_flag="--model",
    models=("sonnet", "opus", "haiku"),
    permission_modes={
        "": (),  # default: interactive permission prompts
        "default": ("--permission-mode", "default"),
        "acceptEdits": ("--permission-mode", "acceptEdits"),
        "plan": ("--permission-mode", "plan"),
        "bypassPermissions": ("--dangerously-skip-permissions",),
    },
))

register(RuntimeAdapter(
    id="copilot-cli",
    label="GitHub Copilot CLI",
    base_argv=("copilot",),
    model_flag="--model",
    models=("gpt-5", "claude-sonnet-4.5"),
    permission_modes={
        "": (),
        "default": (),
        "allowAll": ("--allow-all-tools",),
    },
))

register(RuntimeAdapter(
    id="codex-cli",
    label="Codex CLI",
    base_argv=("codex",),
    model_flag="--model",
    models=("gpt-5-codex", "o4-mini"),
    permission_modes={
        "": (),
        "default": ("--ask-for-approval", "on-request"),
        "auto": ("--ask-for-approval", "on-failure"),
        "full-auto": ("--ask-for-approval", "never", "--sandbox", "workspace-write"),
    },
))

# ---------------------------------------------------------------------------
# Structured (headless) runtimes (Cut 10): driven via a JSON protocol on stdio
# instead of a PTY, so the web UI renders a chat surface with 0-RTT local
# input and streaming events. Adding one is still a single register() call.
#
# Claude Code headless:
#   claude -p --output-format stream-json --input-format stream-json
#          --include-partial-messages --verbose [--permission-mode ...]
# The default permission mode ("") accepts edits for this session per the
# product decision "信任此会话/自动接受编辑"; callers can still request a
# stricter mode. --verbose is required by Claude when output-format is
# stream-json in -p mode.
# ---------------------------------------------------------------------------
register(RuntimeAdapter(
    id="claude-code-structured",
    label="Claude Code (chat)",
    base_argv=(
        "claude", "-p",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ),
    model_flag="--model",
    models=("sonnet", "opus", "haiku"),
    structured=True,
    permission_modes={
        # Default trusts this session (auto-accept edits) per product decision.
        "": ("--permission-mode", "acceptEdits"),
        "acceptEdits": ("--permission-mode", "acceptEdits"),
        "default": ("--permission-mode", "default"),
        "plan": ("--permission-mode", "plan"),
        "bypassPermissions": ("--dangerously-skip-permissions",),
    },
))

register(RuntimeAdapter(
    id="copilot-cli-structured",
    label="GitHub Copilot (chat)",
    # Copilot's ``-p`` runs one prompt then exits, emitting newline-delimited
    # JSON. Each user turn spawns a fresh process (per_turn=True); the prompt
    # text is appended after ``prompt_argv``. Context does not currently carry
    # across turns (stateless v1) — a future cut can share ``--session-id``.
    base_argv=(
        "copilot",
        "--output-format", "json",
        "--stream", "on",
        "--no-color",
    ),
    model_flag="--model",
    models=("gpt-5", "claude-sonnet-4.5"),
    structured=True,
    per_turn=True,
    prompt_argv=("-p",),
    permission_modes={
        # Non-interactive turns require tool auto-approval; make it the default.
        "": ("--allow-all-tools",),
        "allowAll": ("--allow-all-tools",),
        "allowAllPaths": ("--allow-all-tools", "--allow-all-paths"),
    },
))

