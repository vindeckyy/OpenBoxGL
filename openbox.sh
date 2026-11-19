#!/bin/bash
# Desktop sessions open the native host by default, which renders the one UI
# in a WebKitGTK window. Pass --web to fall back to the loopback web server in
# the default browser (development).
set -euo pipefail

# Resolve the share dir. Works from the repo (script and app files colocated),
# from a Makefile/Flatpak install (script in $BINDIR, app files in
# $SHAREDIR/openbox), and inside the AppImage ($APPDIR).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${APPDIR:-}" ]; then
  SHARE="$APPDIR/usr/share/openbox"
else
  SHARE="$SCRIPT_DIR"
  for candidate in "$SCRIPT_DIR" "$SCRIPT_DIR/../share/openbox" "/usr/local/share/openbox"; do
    if [ -f "$candidate/index.html" ] || [ -f "$candidate/web_app.py" ]; then
      SHARE="$candidate"
      break
    fi
  done
fi

# Resolve the Rust openbox-server binary unless OPENBOX_BACKEND=python is set
# for side-by-side soak testing (ADR 0115).
SERVER_BIN=""
if [ "${OPENBOX_BACKEND:-}" != "python" ]; then
  for candidate in \
    "${OPENBOX_SERVER_BIN:-}" \
    "$SHARE/openbox-server" \
    "$SCRIPT_DIR/openbox-server" \
    "$SCRIPT_DIR/target/release/openbox-server" \
    "$(command -v openbox-server 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      SERVER_BIN="$candidate"
      break
    fi
  done
fi

run_backend()
{
  if [ -n "$SERVER_BIN" ]; then
    OPENBOX_ROOT="$SHARE" exec "$SERVER_BIN" "$@"
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "OpenBox needs either openbox-server or Python 3.11+ (Homebrew: brew install python@3.12)." >&2
    exit 1
  fi
  exec python3 -B "$SHARE/web_app.py" "$@"
}

# Library maintenance runs headless: no server, no window.
for arg in "$@"; do
  case "$arg" in
    --check-library|--repair-library|--migrate-library|--rollback-library)
      run_backend "$@" ;;
  esac
done
if [ "${1:-}" = "--web" ]; then
  shift
  run_backend "$@"
fi

# An app started from the Finder inherits a minimal PATH, so the Homebrew
# and MacPorts bins would be invisible to the launcher and the emulators.
if [ "$(uname -s)" = "Darwin" ]; then
  for tool_dir in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /usr/local/sbin /opt/local/bin; do
    case ":$PATH:" in
      *":$tool_dir:"*) ;;
      *) PATH="$tool_dir:$PATH" ;;
    esac
  done
  export PATH
fi

if [ -n "$SERVER_BIN" ]; then
  export OPENBOX_SERVER_BIN="$SERVER_BIN"
  export OPENBOX_ROOT="$SHARE"
elif ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "OpenBox needs either openbox-server or Python 3.11+ (Homebrew: brew install python@3.12), or the OpenBox.app bundle." >&2
  exit 1
fi

# Locate the native launcher: the repo keeps openbox-native.sh beside this
# script; a Makefile/Flatpak install renames it to openbox-native in the same
# $BINDIR. Fall back to PATH for custom prefixes, then to the web app.
for candidate in "$SCRIPT_DIR/openbox-native.sh" "$SCRIPT_DIR/openbox-native" "$(command -v openbox-native 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    exec "$candidate" "$@"
  fi
done

echo "openbox-native launcher not found; falling back to the web app." >&2
run_backend "$@"
