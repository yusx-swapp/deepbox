# Onboarding: bootstrap, roles, invitations (P1 Cut 1)

This document is the operator reference for how the **first owner** is created
and how additional users are onboarded. It covers the exact routes, environment
variables, the threat model, and step-by-step operator instructions.

## Roles

There are exactly two roles:

- `owner` — full control: mint/list/revoke invitations, list users, disable and
  re-enable members.
- `member` — a regular user created via an invitation (or via dev registration).

The first bootstrap user is an `owner`. Every invitee is a `member`.

These are **account administration roles**, distinct from Cut 8 workspace roles. Every user gets a
personal workspace with workspace role `owner`; a workspace owner/admin can add existing enabled
users as `viewer`, `operator`, `admin`, or `owner`. Workspace roles control resource visibility and
terminal input, but never grant global invitation/user administration. See
[`implementation.md`](implementation.md#72-cut-8workspace角色与协作).

### Upgrading a pre-onboarding database

The additive migration gives legacy users the `member` role; it deliberately does
**not** guess which existing account should become Owner. Before exposing an
upgraded instance, stop the app and promote one verified account directly in its
SQLite database:

```sql
UPDATE user SET role = 'owner' WHERE username = '<verified-user>';
```

Back up the database first, verify exactly one intended row changed, and then
restart the app. Do not enable public registration or try to use bootstrap on a
database that already contains users.

## Environment variables

| Variable | Purpose |
|---|---|
| `DEEPBOX_BOOTSTRAP_TOKEN` | One-time token used to create the first owner. Only its SHA-256 hash is held in `Settings`; the plaintext env var is cleared from the process after load and is never stored, logged, or echoed. Remove after first-owner setup. |
| `DEEPBOX_REGISTRATION_ENABLED` | Self-service `/api/auth/register`. **Production must keep this `false`.** Invitations are the production onboarding mechanism. Defaults to `false` in production, `true` in development. |

## Routes

| Method | Route | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/api/auth/bootstrap-status` | none | Safe boolean `{"available": bool}`. Never echoes the token/hash. |
| `POST` | `/api/auth/bootstrap` | bootstrap token | Create the first owner exactly once. Generic `404` for any invalid/unavailable case. |
| `POST` | `/api/auth/register` | none / invite | Dev self-register (when enabled) or redeem an invite (`invite_code`). Invitees become members. |
| `POST` | `/api/invitations` | owner | Mint an invitation with bounded TTL. Returns plaintext token **exactly once**. |
| `GET`  | `/api/invitations` | owner | List invitation metadata (no token/hash). |
| `DELETE` | `/api/invitations/{id}` | owner | Revoke an unredeemed invitation. |
| `GET`  | `/api/users` | owner | List users. |
| `POST` | `/api/users/{id}/disable` | owner | Disable a member. Cannot leave zero enabled owners. |
| `POST` | `/api/users/{id}/enable` | owner | Re-enable a user. |

## Threat model

- **Token secrecy.** The bootstrap token and invitation tokens exist in plaintext
  only transiently: the bootstrap token plaintext never leaves the operator/env
  and is dropped after config load; an invitation's plaintext is returned exactly
  once at mint time. The database stores **only SHA-256 hashes**. Logs never
  contain tokens or hashes.
- **Single first owner.** Bootstrap is guarded by a persistent, concurrency-safe
  latch: the `bootstrap_state` singleton row (`id=1`) is inserted in the **same
  transaction** as the owner user. The unique primary key means that under
  concurrent attempts exactly one commit wins; the others fail the unique
  constraint and receive a generic `404`. Any pre-existing user also makes
  bootstrap unavailable.
- **No enumeration.** Wrong bootstrap token, already-bootstrapped, existing
  users, and malformed input all return an identical generic `404 not found`.
- **One-time invitations.** Redemption is a single conditional `UPDATE` that
  requires `redeemed_at IS NULL AND revoked_at IS NULL AND expires_at > <claim
  time>`, so an invite is single-use, cannot be redeemed after expiry, and cannot
  be redeemed after revoke. Invalid/expired/used codes return a generic `404`.
- **Account disable.** Disabled users cannot log in or use existing browser
  sessions. Active browser and connector WebSockets are closed with code `4001`,
  and connector bearer tokens owned by the account stop authorizing requests.
  The system refuses to disable the last enabled owner (including self-lockout).

## Operator steps

### First deploy — create the first owner

1. In production keep `DEEPBOX_REGISTRATION_ENABLED=false`.
2. Set `DEEPBOX_BOOTSTRAP_TOKEN` to a long random value in the Server Host `.env`.
3. Start the server. `GET /api/auth/bootstrap-status` reports `available: true`
   while there are no users.
4. Open the web UI at `/`; the first-owner setup form appears. Enter the
   bootstrap token, username, and password to create the owner.
5. **Remove `DEEPBOX_BOOTSTRAP_TOKEN`** from the environment and restart. Once a
   user exists, bootstrap is permanently unavailable regardless of the token.

### Inviting members

1. As owner, open the **Owner** section in the web UI.
2. Mint an invitation (optional note, bounded TTL in hours). The plaintext code
   and a prefilled invite URL (`/#invite=<code>`) are shown **once** — copy them.
3. Send the URL to the invitee. The code is carried in the URL fragment, which is
   never sent to HTTP access logs, and is removed from the address bar as soon as
   the page loads. The invite form stays prefilled in memory; the invitee chooses
   a username/password and registers as a member.
4. Revoke unused invitations any time from the same section.

### Managing members

- List users, disable a misbehaving member (their sessions immediately stop
  working), and re-enable later. Owners cannot disable the last enabled owner.
