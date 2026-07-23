#!/usr/bin/env bash
# Install the deepbox local command once (macOS / Linux).
#
# The installer downloads the connector, creates an isolated virtualenv, and
# adds ~/.deepbox/bin/deepbox to the current user's shell PATH. Installation and
# explicit upgrades refresh ~/.deepbox/app; daily `deepbox connect` calls do not.
#
# Run once:
#     curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
#
# Then set DEEPBOX_SERVER_URL and DEEPBOX_TOKEN and run:
#     deepbox connect
#
# Upgrade explicitly with `deepbox upgrade`. The token is never written to disk;
# it is passed to the connector process through the environment only.
#
# Requires Python 3.10+. Claude Code / Copilot CLI / Codex are not installed by
# this script.
set -euo pipefail

say()  { printf '\033[36m[deepbox]\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m[deepbox]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[deepbox]\033[0m %s\n' "$1"; }

SOURCE_ZIP="${DEEPBOX_SOURCE_ZIP:-https://github.com/yusx-swapp/deepbox/archive/refs/heads/main.zip}"
ROOT="${DEEPBOX_HOME:-${HOME}/.deepbox}"
SRC="${ROOT}/app"
VENV="${ROOT}/venv"
BIN="${ROOT}/bin"
COMMAND="${BIN}/deepbox"
LAUNCHER="${ROOT}/deepbox-connect.sh"  # legacy compatibility
INSTALL_SCRIPT_URL="https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh"

say "Installing into ${ROOT}"
mkdir -p "${ROOT}"

# --- 1. Locate Python 3.10+ -------------------------------------------------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  warn "Python 3.10+ was not found on PATH."
  echo  "  Install it, then re-run this installer, e.g.:"
  echo  "    macOS:  brew install python@3.12"
  echo  "    Debian: sudo apt-get install -y python3 python3-venv python3-pip"
  exit 1
fi
ok "Using Python: $($PY --version 2>&1)"

# --- 2. Download + extract connector source --------------------------------
say "Downloading connector source ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ZIP="${TMP}/src.zip"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$SOURCE_ZIP" -o "$ZIP"
else
  wget -qO "$ZIP" "$SOURCE_ZIP"
fi

EXTRACT="${TMP}/x"
mkdir -p "$EXTRACT"
"$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$ZIP" "$EXTRACT"

# GitHub zips nest everything under a single <repo>-<branch> directory.
INNER="$(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -n "$INNER" ] || { warn "Unexpected archive layout."; exit 1; }

rm -rf "$SRC"
mkdir -p "$SRC"
cp -R "${INNER}/connector" "${SRC}/connector"
for f in requirements-connector.txt requirements.txt; do
  [ -f "${INNER}/${f}" ] && cp "${INNER}/${f}" "${SRC}/${f}"
done
ok "Connector source ready."

# --- 3. Virtualenv + dependencies ------------------------------------------
VENV_PY="${VENV}/bin/python"
if [ ! -x "$VENV_PY" ]; then
  say "Creating virtual environment ..."
  "$PY" -m venv "$VENV"
fi
say "Installing connector dependencies ..."
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null
if [ -f "${SRC}/requirements-connector.txt" ]; then
  "$VENV_PY" -m pip install --quiet -r "${SRC}/requirements-connector.txt"
else
  "$VENV_PY" -m pip install --quiet 'httpx>=0.27' 'websockets>=12.0'
fi
SITE_PACKAGES="$("$VENV_PY" -c 'import site; print(site.getsitepackages()[0])')"
if [ -z "$SITE_PACKAGES" ]; then
  warn "Could not locate the connector virtualenv site-packages directory."
  exit 1
fi
printf '%s\n' "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent / 'app'))" > "${SITE_PACKAGES}/deepbox-app.pth"
ok "Dependencies installed."

# --- 4. Install stable command + legacy launcher ----------------------------
# This shim is not rewritten during upgrades, so it can safely invoke the
# installer while the command file itself is running.
mkdir -p "$BIN"
if [ ! -e "$COMMAND" ]; then
  cat > "$COMMAND" <<'EOF'
#!/usr/bin/env bash
# deepbox-stable-shim-v1
set -euo pipefail
if [ -n "${DEEPBOX_HOME:-}" ]; then
  ROOT="$DEEPBOX_HOME"
else
  COMMAND_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  ROOT="$(CDPATH= cd -- "${COMMAND_DIR}/.." && pwd -P)"
fi
export DEEPBOX_HOME="$ROOT"
if [ "${1:-}" = "upgrade" ]; then
  export DEEPBOX_INSTALL_ONLY=1
  URL="https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" | bash
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$URL" | bash
  else
    echo "[deepbox] curl or wget is required for upgrade." >&2
    exit 1
  fi
  exit
fi
PY="${ROOT}/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[deepbox] installation is incomplete; run deepbox upgrade" >&2
  exit 1
fi
exec "$PY" -I -u -m connector.cli "$@"
EOF
elif ! grep -Fq 'deepbox-stable-shim-v1' "$COMMAND"; then
  warn "Refusing to replace an unrecognized command at ${COMMAND}."
  exit 1
fi
chmod +x "$COMMAND"
ok "Command installed: ${COMMAND}"

cat > "$LAUNCHER" <<'EOF'
#!/usr/bin/env bash
# Legacy compatibility; prefer: deepbox connect
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export DEEPBOX_HOME="$ROOT"
exec "${ROOT}/bin/deepbox" connect "$@"
EOF
chmod +x "$LAUNCHER"

# Add the command to the profile used by the current login shell. A marker keeps
# repeated explicit upgrades from changing the profile again.
case "$(basename "${SHELL:-sh}")" in
  zsh) PROFILE="${HOME}/.zprofile" ;;
  bash)
    if [ -f "${HOME}/.bash_profile" ]; then PROFILE="${HOME}/.bash_profile"
    else PROFILE="${HOME}/.profile"
    fi
    ;;
  *) PROFILE="${HOME}/.profile" ;;
esac
PATH_MARKER='# deepbox command path v2'
if ! grep -Fq "$PATH_MARKER" "$PROFILE" 2>/dev/null; then
  BIN_LITERAL="$("$PY" -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$BIN")"
  {
    printf '\n%s\n' "$PATH_MARKER"
    printf '_DEEPBOX_BIN=%s\n' "$BIN_LITERAL"
    cat <<'EOF'
case ":${PATH}:" in
  *":${_DEEPBOX_BIN}:"*) ;;
  *) export PATH="${_DEEPBOX_BIN}:${PATH}" ;;
esac
unset _DEEPBOX_BIN
EOF
  } >> "$PROFILE"
fi

# --- 5. Finish, or honor commands generated by the previous web UI ---------
SERVER="${DEEPBOX_SERVER_URL:-}"
TOKEN="${DEEPBOX_TOKEN:-}"
INSTALL_ONLY="${DEEPBOX_INSTALL_ONLY:-0}"
if [ "$INSTALL_ONLY" != "1" ] && [ -n "$SERVER" ] && [ -n "$TOKEN" ]; then
  ok "Setup complete. Connecting ..."
  echo
  echo "  Reconnect without reinstalling:"
  echo "      deepbox connect"
  echo
  "$COMMAND" doctor || true
  rm -rf "$TMP"
  trap - EXIT
  exec "$COMMAND" connect
else
  if { [ -n "$SERVER" ] && [ -z "$TOKEN" ]; } || { [ -n "$TOKEN" ] && [ -z "$SERVER" ]; }; then
    warn "Both DEEPBOX_SERVER_URL and DEEPBOX_TOKEN are required to connect."
  fi
  ok "Setup complete."
  echo "  Open a new terminal if needed, then run:"
  echo "      deepbox connect"
  echo "  Upgrade later with:"
  echo "      deepbox upgrade"
fi
