"""SQLite backup and restore tooling for DeepBox operators.

The server stores everything in a single SQLite database. This module provides
safe, scriptable backup and restore primitives plus a CLI:

* **Backup** uses SQLite's online backup API so a consistent snapshot can be
  taken while the server is running, then validates the copy with
  ``PRAGMA integrity_check`` before declaring success.
* **Restore** validates the incoming file and requires an explicit ``--force``
  acknowledgement before replacing an existing database. Process detection is
  intentionally not guessed: SQLite may have no active lock while a server is
  idle. The operator must stop the server first, then confirm the replacement.

Nothing here touches models, keys, or any remote environment; it only moves a
local file around.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Optional


class BackupError(RuntimeError):
    """Raised for any recoverable backup/restore failure."""


def sqlite_path_from_url(database_url: str) -> Path:
    """Return the file path for a ``sqlite:///`` URL.

    Raises :class:`BackupError` for non-file SQLite URLs (in-memory) or other
    database backends, which cannot be file-copied.
    """

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise BackupError(f"not a SQLite file URL: {database_url!r}")
    raw = database_url[len(prefix) :]
    if not raw or raw == ":memory:":
        raise BackupError("cannot back up an in-memory database")
    return Path(raw)


def integrity_ok(db_path: Path) -> bool:
    """Return True if ``PRAGMA integrity_check`` reports 'ok'."""

    if not db_path.exists():
        return False
    conn = sqlite3.connect(os.fspath(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
    return bool(row) and row[0] == "ok"


def is_sqlite_file(path: Path) -> bool:
    """Cheap header sniff: a real SQLite DB starts with a known magic string."""

    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError:
        return False
    return header.startswith(b"SQLite format 3\x00")


def _timestamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_database(database_url: str, dest_dir: Path) -> Path:
    """Take a validated online backup of the database into ``dest_dir``.

    Returns the path to the created backup file.
    """

    src = sqlite_path_from_url(database_url)
    if not src.exists():
        raise BackupError(f"database file does not exist: {src}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"deepbox-backup-{_timestamp()}.db"

    source = sqlite3.connect(os.fspath(src))
    try:
        target = sqlite3.connect(os.fspath(dest))
        try:
            # Online backup API: consistent snapshot even under concurrent writes.
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    if not integrity_ok(dest):
        dest.unlink(missing_ok=True)
        raise BackupError("backup failed integrity check; removed corrupt copy")
    return dest


def restore_database(
    database_url: str,
    backup_file: Path,
    *,
    force: bool = False,
) -> Path:
    """Atomically restore ``backup_file`` over the live database.

    Safety gates, in order:

    1. The backup file must look like SQLite and pass an integrity check.
    2. Replacing an existing database requires ``force=True``. This explicit
       acknowledgement is required even when SQLite has no current write lock,
       because an idle server cannot be detected reliably from the DB file.
    3. The current database is preserved as a ``.pre-restore`` sidecar.
    4. The new file is copied to a temp path on the same volume then
       ``os.replace``-d into position (atomic on POSIX and Windows).
    """

    dest = sqlite_path_from_url(database_url)

    if not backup_file.exists():
        raise BackupError(f"backup file not found: {backup_file}")
    if not is_sqlite_file(backup_file):
        raise BackupError(f"not a SQLite database: {backup_file}")
    if not integrity_ok(backup_file):
        raise BackupError(f"backup failed integrity check: {backup_file}")

    if dest.exists() and not force:
        raise BackupError(
            "refusing to replace an existing database: stop the server, then "
            "pass --force to acknowledge the restore"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Preserve the current database before overwriting it.
    if dest.exists():
        sidecar = dest.with_suffix(dest.suffix + ".pre-restore")
        shutil.copy2(os.fspath(dest), os.fspath(sidecar))

    tmp = dest.with_suffix(dest.suffix + ".restore-tmp")
    shutil.copy2(os.fspath(backup_file), os.fspath(tmp))
    os.replace(os.fspath(tmp), os.fspath(dest))
    return dest


def _load_database_url(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env = os.getenv("DEEPBOX_DATABASE_URL")
    if env:
        return env
    # Match the server default (config.py): a file next to the repo root.
    return "sqlite:///deepbox.db"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DeepBox SQLite backup/restore")
    parser.add_argument(
        "--database-url",
        help="SQLite URL (default: $DEEPBOX_DATABASE_URL or sqlite:///deepbox.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="take a validated online backup")
    b.add_argument("dest_dir", type=Path, help="directory to write the backup into")

    r = sub.add_parser("restore", help="restore a backup over the live database")
    r.add_argument("backup_file", type=Path, help="backup file to restore")
    r.add_argument(
        "--force",
        action="store_true",
        help="acknowledge replacement after stopping the server",
    )

    args = parser.parse_args(argv)
    database_url = _load_database_url(args.database_url)

    try:
        if args.command == "backup":
            path = backup_database(database_url, args.dest_dir)
            print(f"backup written: {path}")
        elif args.command == "restore":
            path = restore_database(
                database_url, args.backup_file, force=args.force
            )
            print(f"database restored: {path}")
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
