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
```

The legacy `~/.deepbox/deepbox-connect.cmd` / `.sh` launcher delegates to
`deepbox connect`, so existing shortcuts continue to work.

### Managing local projects

Project paths live only in connector-local `state.db` under
`%LOCALAPPDATA%\deepbox` on Windows or
`${XDG_STATE_HOME:-~/.local/state}/deepbox` on macOS/Linux. The server and browser
receive a stable project ID and display name, never the path. Register a project
before selecting it while creating an agent in the browser:

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
adding the same path again reuses its ID. `remove` deletes only the local
registration, not the directory. After synchronization, server-side agents that
referenced the removed project lose that binding. `sync` sends the path-free
project list without starting the long-running connector.

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
