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
    if [ -f "$candidate/web_app.py" ]; then
      SHARE="$candidate"
      break
    fi
  done
fi

if [ "${1:-}" = "--web" ]; then
  shift
  exec python3 "$SHARE/web_app.py" "$@"
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
exec python3 "$SHARE/web_app.py" "$@"
