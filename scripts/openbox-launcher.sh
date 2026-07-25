#!/usr/bin/env bash
# Keyboard launcher helper for OpenBox (rofi/wofi/dmenu).
set -euo pipefail

picker="${1:-rofi}"
data_dir="${OPENBOX_DATA_DIR:-$HOME/.local/share/openbox-game-launcher}"
port_file="$data_dir/server.port"
token_file="$data_dir/server.token"

if [[ ! -f "$port_file" ]]; then
  echo "OpenBox is not running. Start the web UI first." >&2
  exit 1
fi

port="$(cat "$port_file")"
token=""
if [[ -f "$token_file" ]]; then
  token="$(cat "$token_file")"
fi

menu=$(
  curl -fsS -H "X-OpenBox-Token: $token" "http://127.0.0.1:${port}/api/launcher/menu" \
    | python3 -c 'import json,sys; print("\n".join("{}\t{}".format(row.get("id",""), row.get("label","")) for row in json.load(sys.stdin).get("items",[])))'
)

if [[ -z "$menu" ]]; then
  echo "Launcher menu is empty." >&2
  exit 1
fi

case "$picker" in
  rofi)
    selection=$(printf '%s\n' "$menu" | rofi -dmenu -i -p OpenBox)
    ;;
  wofi)
    selection=$(printf '%s\n' "$menu" | wofi --dmenu --prompt OpenBox)
    ;;
  *)
    selection=$(printf '%s\n' "$menu" | dmenu -i -p OpenBox)
    ;;
esac

[[ -n "$selection" ]] || exit 0
id="${selection%%$'\t'*}"

case "$id" in
  bigbox) xdg-open "http://127.0.0.1:${port}/?deeplink=bigbox&token=${token}" ;;
  settings) xdg-open "http://127.0.0.1:${port}/?deeplink=settings&token=${token}" ;;
  search)
    query=$(rofi -dmenu -p "Search games" 2>/dev/null || true)
    [[ -n "$query" ]] && xdg-open "http://127.0.0.1:${port}/?deeplink=search&q=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$query")&token=${token}"
    ;;
  launch:*)
  game_id="${id#launch:}"
    curl -fsS -X POST -H "Content-Type: application/json" -H "X-OpenBox-Token: $token" \
      -d "{\"id\":${game_id}}" "http://127.0.0.1:${port}/api/launch" >/dev/null
    ;;
  *)
    xdg-open "http://127.0.0.1:${port}/?token=${token}"
    ;;
esac
