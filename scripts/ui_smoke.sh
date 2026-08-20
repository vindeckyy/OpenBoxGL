#!/bin/bash
# UI smoke test: boots web_app.py against a temp data dir, drives it with
# puppeteer, and asserts the AppState refactor renders a working grid.
set -u
cd "$(dirname "$0")/.."

DATA_DIR=$(mktemp -d /tmp/obx-ui-smoke.XXXXXX)
export OPENBOX_DATA_DIR="$DATA_DIR"

python3 -B web_app.py --no-browser > "$DATA_DIR/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; rm -rf "$DATA_DIR"' EXIT

# wait for the server token
for i in $(seq 1 30); do
  [ -f "$DATA_DIR/server.token" ] && [ -f "$DATA_DIR/server.port" ] && break
  sleep 0.5
done
TOKEN=$(cat "$DATA_DIR/server.token")
PORT=$(cat "$DATA_DIR/server.port")

# seed the library
python3 -B - <<'EOF'
import os
from openbox import save_state
save_state({"games": [
    {"name": "Quake", "platform": "PC", "genre": "FPS", "year": "1996", "developer": "id Software", "path": "/bin/true", "favorite": True, "rating": 5, "progress": "Beaten", "play_count": 12, "playtime_seconds": 5400},
    {"name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "year": "1995", "path": "/bin/true"},
], "profiles": {}, "history": [], "settings": {}, "playlists": []})
print("seeded")
EOF

TOKEN="$TOKEN" PORT="$PORT" node scripts/ui_smoke.cjs
