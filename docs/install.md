# Install once, connect anytime

Install deepbox once as a local command. Daily connections use `deepbox connect`;
they do **not** download code, rebuild the virtualenv, or replace
`~/.deepbox/app`.

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
```

The Windows installer makes `deepbox` available in the current PowerShell
session. On macOS/Linux, open a new terminal after installation or run:

```bash
export PATH="$HOME/.deepbox/bin:$PATH"
```

## Connect a machine

Sign in to the browser, create a devbox, and mint its one-time token. The token
dialog generates the complete command for each platform:

```powershell
# Windows
$env:DEEPBOX_SERVER_URL = 'https://deepbox.example'
$env:DEEPBOX_TOKEN = 'hpc_box_xxxxxxxx'
deepbox connect
```

```bash
# macOS / Linux
export DEEPBOX_SERVER_URL='https://deepbox.example'
export DEEPBOX_TOKEN='hpc_box_xxxxxxxx'
deepbox connect
```

`deepbox connect` runs from the caller's current directory and only starts the
already-installed connector. It never invokes either installer. The token is
passed through the process environment and is never written to disk by deepbox.

## What the one-time installer does

1. Finds a Python 3.10+ interpreter (and prints install guidance if missing).
2. Downloads the connector source ZIP from the public `yusx-swapp/deepbox`
   mirror, anonymously and without Git credentials.
3. During a Windows install or explicit upgrade, finds connector processes from
   this installation's virtualenv and stops their connector-owned process trees
   before replacing the source directory.
4. Refreshes `~/.deepbox/app`, creates or reuses `~/.deepbox/venv`, installs
   the connector dependencies (`httpx`, `websockets`, and `pywinpty` on
   Windows), and records the app location in the venv's `deepbox-app.pth`.
5. Installs a stable command at `~/.deepbox/bin/deepbox.cmd` on Windows or
   `~/.deepbox/bin/deepbox` on macOS/Linux. The shim starts Python with `-I`,
   so the caller's working directory and `PYTHONPATH` cannot replace the
   installed connector package; the installer then adds the bin directory to
   the user's PATH.
6. Writes `~/.deepbox/deepbox-connect.cmd` or `.sh` as a compatibility alias for
   existing installations.

If both `DEEPBOX_SERVER_URL` and `DEEPBOX_TOKEN` are already set, the installer
runs diagnostics and connects after setup. Otherwise it installs only and tells
the user to run `deepbox connect`.

Everything managed by the installer lives under `~/.deepbox`. Set
`DEEPBOX_HOME` while installing to use a different root. The stable command
resolves that root from its own `bin` location when the variable is absent, and
the Unix installer records the actual bin path in the selected login profile.
`DEEPBOX_SOURCE_ZIP` installs from a fork or pinned branch.

## Reconnect and local commands

Once installed, use the same command from any working directory:

```text
deepbox connect
deepbox doctor
deepbox status
deepbox project add <path> [--name <display-name>]
deepbox project list
deepbox project remove <project-id>
deepbox project sync
deepbox skill install <folder> [--project [ID|NAME|PATH]] [--force]
deepbox skill list [--project [ID|NAME|PATH]]
deepbox skill inspect <name> [--project [ID|NAME|PATH]]
deepbox skill remove <name> [--project [ID|NAME|PATH]] [--force]
```

The legacy `~/.deepbox/deepbox-connect.cmd` / `.sh` launcher delegates to
`deepbox connect`, so existing shortcuts continue to work.

## Local projects and skills

### Managing local projects

Project paths live only in connector-local `state.db` under
`%LOCALAPPDATA%\deepbox` on Windows or
`${XDG_STATE_HOME:-~/.local/state}/deepbox` on macOS/Linux. The server and browser
receive a stable project ID, display name, and non-secret runtime config, never
the path. Register a project before selecting it while creating an agent:

```powershell
deepbox project add "C:\src\my-repo" --name "my-repo"
deepbox project list
deepbox project remove <project-id>
deepbox project sync
```

```bash
deepbox project add "$HOME/src/my-repo" --name "my-repo"
deepbox project list
deepbox project remove <project-id>
deepbox project sync
```

`add` requires an existing directory and stores its canonical absolute path;
adding the same path again reuses its ID. `remove` deletes only the registration,
not the directory. With a token, `add` and `remove` immediately report the path-free
inventory; otherwise run `sync` later. A project with a managed project-scoped
skill cannot be removed until that skill is removed.

The browser's **Add agent** form refreshes projects at the point of use. Its
**Add a local project** action only builds and copies a command such as
`deepbox project add "C:\src\my-repo" --name "my-repo"`; it never browses or
mutates the workstation. Run the command locally and then choose **Refresh projects**.

### `SKILL.md` package schema

A skill is a UTF-8 directory tree with a `SKILL.md` file at its root. The file
starts with YAML frontmatter containing string `name` and `description` fields,
followed by normal Markdown instructions:

```markdown
---
name: review-pr
description: Review a pull request for correctness, tests, and operational risk.
---
# Review a pull request

Read the changed files before reporting findings.
```

The directory basename must exactly equal `name`. Names use lower-kebab-case,
are at most 64 characters, and descriptions are at most 1,024 characters.
Deepbox decodes `SKILL.md` strictly as UTF-8, parses frontmatter with
`yaml.safe_load`, and requires a mapping with string keys. A package may contain
at most 256 regular files and 10 MiB total. Symlinks, junctions, other reparse
points, traversal, and files that change during hashing are rejected.
Script-looking files and a `scripts/` directory are allowed but set
`contains_scripts`; Deepbox itself never executes any skill content.

### Install and manage skills

Personal scope is the default. For project scope, provide `--project` with a
registered ID, unique case-insensitive name, or exact normalized path. Supplying
`--project` without a value is equivalent to `--project .` and selects the
longest registered project containing the current working directory.

```powershell
# Personal scope
deepbox skill install "C:\Skills\review-pr"
deepbox skill list
deepbox skill inspect review-pr
deepbox skill remove review-pr

# Project scope
deepbox skill install "C:\Skills\review-pr" --project "my-repo"
deepbox skill list --project "my-repo"
deepbox skill inspect review-pr --project "my-repo"
deepbox skill remove review-pr --project "my-repo"
```

Install validates and hashes the source twice, copies it to
`<connector-state-root>/skills/store/<digest>/<name>/`, then stages replacements
in every personal or project skill root declared by the registered runtime adapter
families. The local database records scope, project, targets, and binding paths.
Repeated root discovery merges with earlier bindings instead of orphaning them.
`list` and `inspect` verify both the store and every binding and return
`installed`, `drifted`, or `missing`. Install and remove refuse drifted
destinations unless `--force` is explicit. Removing the final reference also
garbage-collects the content-addressed store directory.

While connected, the connector reports inventory changes automatically. The
server stores only `id`, `name`, `description`, `digest`, `scope`, `project_id`,
`targets`, `contains_scripts`, and `status`; it receives no source, store,
project, or binding path and never reads model credentials.

### Structured chat controls

Opening **Add agent** refreshes runtime capabilities and projects before rendering
its selectors. For an installed runtime, failed or empty live model discovery
falls back to the adapter family's static models with `models.status=partial` and
`models.source=adapter`. Structured chat always includes **Runtime default**.
Adapters that set `allow_custom_models` receive an editable model combobox; other
model and reasoning controls remain selects.

Session-scoped controls are editable until the session is configured or contains
its first chat item. They then lock with a prompt to start **New chat**. That action
sends the structured `terminate` frame, creates a blank persisted session, and
re-enables the controls without deleting saved history. The server forwards
termination only for an operator who holds the keyboard lease.

## Upgrade explicitly

Upgrade only when requested:

```text
deepbox upgrade
```

The stable command downloads the current installer with
`DEEPBOX_INSTALL_ONLY=1`. The installer may stop a running connector and refresh
`~/.deepbox/app`; normal `deepbox connect` calls never do this. After an upgrade,
run `deepbox connect` again if the previous connector was stopped.

### Windows process safety during install or upgrade

`install.ps1` matches only this installation's virtualenv Python running
`-m connector` or `-m connector.cli`. It snapshots and stops that process's
connector-owned child tree, waits for handles to be released, and retries the
source-directory replacement. It does not stop unrelated Python processes and
never logs inspected command lines.

If a separate shell has manually changed its working directory to
`~\.deepbox\app`, leave that directory or close the shell before upgrading.

## Hosting the installer scripts

The browser and examples use these anonymous GitHub Raw endpoints on `main`:

```text
https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1
https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh
```

The scripts are published by pushing the same reviewed commit to the public
GitHub mirror; there is no separate Blob upload step. Keep both scripts in that
commit so the UI command and downloaded source stay in sync.

Verify both endpoints anonymously after publishing:

```powershell
Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | Select-Object StatusCode
```

```bash
curl -I https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh
```

Both should return HTTP `200`. GitHub Raw may cache briefly after a push; pin a
commit SHA in the URL when an immutable installer is required.
