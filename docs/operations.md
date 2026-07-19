# Operations runbook (minimal production ops)

This runbook covers the day-2 operational surface added in **Cut 3 — Minimal
production ops**: structured logs, connector visibility, readiness, backup and
restore, capacity monitoring, version reporting, and a post-restart smoke check.

The server never handles models or provider keys and never performs live
environment operations. Everything here operates on the local server process and
its single SQLite database.

## Structured JSON logs

The server emits one JSON object per line via `server/app/logging.py`. Each line
carries at least `ts` (ISO-8601 UTC, `Z` suffix), `level`, `logger`, and
`message`; structured events add an `event` field plus context.

Configure verbosity with `DEEPBOX_LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`).
Logs go to stdout, which Azure App Service and local shells both capture. No
secrets are logged: event helpers drop `None` fields and the app never passes
tokens or password material into log context.

Key events:

| event | meaning |
| --- | --- |
| `connector.online` | a connector opened the `/ws/devbox` socket |
| `connector.offline` | the connector socket closed |
| `connector.heartbeat` | a heartbeat ping was received (DEBUG) |
| `capacity.threshold` | a capacity check crossed warn/alert |

Security audit records are also one-line JSON (`server/app/audit.py`). They carry an
event name, outcome, actor/resource metadata and optional request context; nested
password/token/secret/cookie/authorization/API-key values are recursively redacted,
and audit emission never interrupts the request path. Cut 8 emits `workspace.created`,
`workspace.member_added`, `workspace.role_changed`, `workspace.member_removed`,
`keyboard.acquired`, `keyboard.requested`, `keyboard.released` and `keyboard.handed_off`;
actor/resource IDs are metadata only and no terminal payload is added to audit records.

## Workspace collaboration operations

Keyboard ownership is a 30-second database lease. Active holders renew on input and the browser
sends a 20-second heartbeat; the server releases the lease when that user's final socket for the
Session closes. An abandoned lease is therefore automatically reclaimable after TTL without an
operator database edit. A holder can hand off an outstanding request in the terminal header.

The current deployment remains intentionally single-instance: live participant fan-out uses the
in-process Hub and lease decisions use the one App Service SQLite database. Do not scale out above
one instance until Cut 9 supplies shared routing and a shared lease backend. Workspace membership
and lease rows are included in normal SQLite backup/restore.

## Security baseline configuration

Production mode is selected with `DEEPBOX_ENV=production`. Startup then fails closed
unless `DEEPBOX_ALLOWED_ORIGINS` (or `DEEPBOX_PUBLIC_URL`) supplies at least one browser
origin and `DEEPBOX_COOKIE_SECURE=true`. Unsafe cookie-authenticated requests must carry
an allowed `Origin`; `/ws/term` applies the same allowlist. Responses receive
anti-sniffing, framing, referrer, permissions and CSP headers; production responses
also receive HSTS.

Rate limiting defaults on in production and can be controlled with:

* `DEEPBOX_RATE_LIMIT_ENABLED`
* `DEEPBOX_RATE_LIMIT_API_PER_MINUTE` (default 300)
* `DEEPBOX_RATE_LIMIT_LOGIN_PER_MINUTE` (default 10)
* `DEEPBOX_RATE_LIMIT_TOKEN_PER_MINUTE` (default 20)

Limits use bounded in-process fixed windows keyed by client and route class. Health,
readiness, version and bootstrap-status probes are exempt. With the current single
App Service instance this is process-wide; a future multi-instance deployment must
move the limiter to a shared backend.

## Connector visibility (online / offline / reconnect / heartbeat)

The connector (`connector/client.py`, `PROTOCOL_VERSION = 3`) sends a
`{"type":"heartbeat"}` frame every `HEARTBEAT_INTERVAL` (20s) while connected.
The server refreshes the devbox `last_seen_at` and replies with
`heartbeat_ack`. On reconnect the connector increments a local `connect_count`
and logs the attempt number, so flapping links are visible in connector output.
Server-side, online/offline transitions are logged as structured events (above)
and reflected in agent `presence`.

## Readiness: `GET /api/ready`

Verifies the database answers `SELECT 1` and that the data directory exists and
is writable (`os.access(..., W_OK)`), returning `503` on failure. It performs no
mutations beyond `mkdir` of the data dir and never echoes secrets or connection
strings. Each probe also evaluates capacity and emits edge-triggered
`capacity.threshold` / `capacity.recovered` events when status changes; it does
not repeat one warning per probe, and capacity pressure does not itself make the
service unready. Use it as the platform health-check path (already wired in
`infra/main.bicep`).

`GET /api/health` is a cheaper liveness probe returning `{status, protocol_version}`.

## Version / build provenance

* `GET /api/version` — **public-safe**. Returns only `{version, commit}` (short
  commit). No paths, branch, or dirty state.
* `GET /api/admin/version` — **owner-only**. Adds full commit, short commit, and
  working-tree `dirty` flag.

In deployed artifacts without a `.git` directory, set `DEEPBOX_GIT_COMMIT` at
build time; the server prefers it over shelling out to git. The checked-in
`scripts/deploy-azure.ps1` resolves `git rev-parse HEAD` and passes the value
through Bicep automatically.

## Capacity monitoring

`GET /api/admin/capacity` (owner-only) reports `ok` / `warn` / `alert` for:

* **database** — SQLite file size in MB (larger is worse), thresholds
  `DEEPBOX_DB_SIZE_WARN_MB` / `DEEPBOX_DB_SIZE_ALERT_MB`.
* **recording_disk_free** — free MB on the recordings volume (smaller is worse),
  thresholds `DEEPBOX_DISK_FREE_WARN_MB` / `DEEPBOX_DISK_FREE_ALERT_MB`.

The overall status is the worst of the resources. Crossing a threshold logs a
`capacity.threshold` event at WARNING. Threshold ordering is validated at
startup (DB alert must be >= DB warn; disk-free alert must be <= disk-free warn).

## Backup and restore

Tooling lives in `server/ops/backup.py` and is safe to run from cron or by hand.

Take a validated online backup (safe while the server runs):

```powershell
py -m server.ops.backup --database-url sqlite:///C:/deepbox-data/deepbox.db backup C:/deepbox-data/backups
```

It uses SQLite's online backup API for a consistent snapshot and runs
`PRAGMA integrity_check` on the copy, deleting it if the check fails.

Restore a backup over the live database only after stopping the server:

```powershell
py -m server.ops.backup --database-url sqlite:///C:/deepbox-data/deepbox.db restore C:/deepbox-data/backups/deepbox-backup-YYYYMMDDTHHMMSSZ.db --force
```

Safety gates:

1. The backup must have a valid SQLite header and pass `integrity_check`.
2. SQLite cannot reliably reveal an idle live server, so replacing any existing
   database is **refused** unless `--force` is supplied. Stop the server first;
   `--force` is the explicit acknowledgement of that operator step.
3. The current database is copied to a `*.pre-restore` sidecar before the swap.
4. The new file is written to a temp path on the same volume then
   `os.replace`-d into position (atomic), so a crash mid-restore cannot corrupt
   the live database.

`--database-url` defaults to `$DEEPBOX_DATABASE_URL`, then `sqlite:///deepbox.db`.

## Post-restart smoke check

After a deploy or restart, run:

```powershell
py -m server.ops.smoke --base-url https://your-server.example
```

It exercises `/api/health`, `/api/ready`, and `/api/version`, prints a
PASS/FAIL line per check, and exits non-zero on any failure so it can gate a
deployment pipeline. All three endpoints are unauthenticated, so no secrets are
needed to run it.
