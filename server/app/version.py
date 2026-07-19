"""Version and build-provenance reporting.

Operators need to answer "what exactly is running?" after a deploy or restart.
This module resolves a semantic version and the git commit the process was built
from. It is designed to work in three environments:

* a git checkout (local dev) -> read ``git rev-parse``
* an Azure App Service deploy where source is copied without ``.git`` ->
  read the ``DEEPBOX_GIT_COMMIT`` env var injected at build time
* a bare tarball -> fall back to ``"unknown"``

Two views are exposed. :func:`public_version` is safe to serve unauthenticated:
it reveals only the marketing version and a short commit, never file paths,
branch names, or dirty state. :func:`detailed_version` adds operator-only fields
and is intended for authenticated endpoints.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

# Bump on release. Kept here (not in git tags) so a tarball deploy still reports
# something meaningful.
VERSION = "0.1.0"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_git(args: list[str]) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


@lru_cache(maxsize=1)
def git_commit() -> str:
    """Return the full git commit hash, or ``"unknown"``.

    Environment override wins so container images (which usually ship without a
    ``.git`` directory) can report the commit baked in at build time.
    """

    env = os.getenv("DEEPBOX_GIT_COMMIT")
    if env and env.strip():
        return env.strip()
    commit = _run_git(["rev-parse", "HEAD"])
    return commit or "unknown"


@lru_cache(maxsize=1)
def git_dirty() -> bool:
    """Return True if the working tree has uncommitted changes."""

    if os.getenv("DEEPBOX_GIT_COMMIT"):
        # In a deployed artifact there is no working tree to be dirty.
        return False
    status = _run_git(["status", "--porcelain"])
    if status is None:
        return False
    return bool(status.strip())


def short_commit() -> str:
    commit = git_commit()
    if commit == "unknown":
        return commit
    return commit[:12]


def public_version() -> dict:
    """Version info safe for unauthenticated callers."""

    return {"version": VERSION, "commit": short_commit()}


def detailed_version() -> dict:
    """Version info for authenticated operators."""

    return {
        "version": VERSION,
        "commit": git_commit(),
        "commit_short": short_commit(),
        "dirty": git_dirty(),
    }
