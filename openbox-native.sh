#!/bin/bash
# Native host launcher. Runs the WebKitGTK host, which spawns the Python web
# server and renders the one UI in a native window.
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

# Library maintenance runs headless: no native host, no window.
for arg in "$@"; do
  case "$arg" in
    --check-library|--repair-library|--migrate-library|--rollback-library)
      if [ -n "$SERVER_BIN" ]; then
        OPENBOX_ROOT="$SHARE" exec "$SERVER_BIN" "$@"
      fi
      exec "${OPENBOX_PYTHON:-python3}" -B "$SHARE/web_app.py" "$@" ;;
  esac
done
HOST_BIN="${OPENBOX_NATIVE_HOST:-$SHARE/native_host}"

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

run_web_app()
{
  if [ -n "$SERVER_BIN" ]; then
    if [ -n "${OPENBOX_BUNDLED_LIB_PATH:-}" ]; then
      exec env LD_LIBRARY_PATH="$OPENBOX_BUNDLED_LIB_PATH" OPENBOX_ROOT="$SHARE" "$SERVER_BIN" "$@"
    else
      OPENBOX_ROOT="$SHARE" exec "$SERVER_BIN" "$@"
    fi
  fi
  if ! "${OPENBOX_PYTHON:-python3}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "OpenBox needs either openbox-server or Python 3.11+ (Homebrew: brew install python@3.12), or the OpenBox.app bundle." >&2
    exit 1
  fi
  if [ -n "${OPENBOX_BUNDLED_LIB_PATH:-}" ]; then
    exec env LD_LIBRARY_PATH="$OPENBOX_BUNDLED_LIB_PATH" "${OPENBOX_PYTHON:-python3}" -B "$SHARE/web_app.py" "$@"
  else
    exec "${OPENBOX_PYTHON:-python3}" -B "$SHARE/web_app.py" "$@"
  fi
}

if [ ! -x "$HOST_BIN" ]; then
  echo "native_host is missing at $HOST_BIN; falling back to the system-browser app window." >&2
  run_web_app "$@"
fi

export OPENBOX_ROOT="$SHARE"
if [ -n "$SERVER_BIN" ]; then
  export OPENBOX_SERVER_BIN="$SERVER_BIN"
fi
if [ -f "$SHARE/web_app.py" ]; then
  export OPENBOX_WEB_APP="$SHARE/web_app.py"
fi
export OPENBOX_PYTHON="${OPENBOX_PYTHON:-python3}"
if [ -n "${OPENBOX_BUNDLED_LIB_PATH:-}" ]; then
  env LD_LIBRARY_PATH="$OPENBOX_BUNDLED_LIB_PATH" "$HOST_BIN" "$@" && exit 0
else
  "$HOST_BIN" "$@" && exit 0
fi
code=$?
if [ "$(uname -s)" = "Darwin" ]; then
  echo "native_host failed (exit $code). Build the macOS host with scripts/build_native_host_macos.sh." >&2
else
  echo "native_host failed (exit $code). Install webkit2gtk: libwebkit2gtk-4.1-dev on Debian/Ubuntu, webkit2gtk-4.1 on Fedora." >&2
fi
echo "Falling back to the system-browser app window." >&2
run_web_app "$@"
