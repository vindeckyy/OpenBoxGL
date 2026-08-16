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
    if [ -f "$candidate/web_app.py" ]; then
      SHARE="$candidate"
      break
    fi
  done
fi

HOST_BIN="${OPENBOX_NATIVE_HOST:-$SHARE/native_host}"

run_web_app()
{
  if [ -n "${OPENBOX_BUNDLED_LIB_PATH:-}" ]; then
    exec env LD_LIBRARY_PATH="$OPENBOX_BUNDLED_LIB_PATH" "${OPENBOX_PYTHON:-python3}" "$SHARE/web_app.py" "$@"
  else
    exec "${OPENBOX_PYTHON:-python3}" "$SHARE/web_app.py" "$@"
  fi
}

if [ ! -x "$HOST_BIN" ]; then
  echo "native_host is missing at $HOST_BIN; falling back to the system-browser app window." >&2
  run_web_app "$@"
fi

export OPENBOX_WEB_APP="$SHARE/web_app.py"
export OPENBOX_PYTHON="${OPENBOX_PYTHON:-python3}"
if [ -n "${OPENBOX_BUNDLED_LIB_PATH:-}" ]; then
  env LD_LIBRARY_PATH="$OPENBOX_BUNDLED_LIB_PATH" "$HOST_BIN" "$@" && exit 0
else
  "$HOST_BIN" "$@" && exit 0
fi
code=$?
echo "native_host failed (exit $code). Install webkit2gtk: libwebkit2gtk-4.1-dev on Debian/Ubuntu, webkit2gtk-4.1 on Fedora." >&2
echo "Falling back to the system-browser app window." >&2
run_web_app "$@"
