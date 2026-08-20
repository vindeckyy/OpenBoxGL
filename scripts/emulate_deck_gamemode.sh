#!/usr/bin/env bash
# Nested gamescope harness that approximates Steam Deck / Bazzite Game Mode.
# Usage: ./scripts/emulate_deck_gamemode.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GAMESCOPE_BIN="$(command -v gamescope || true)"
if [[ -z "$GAMESCOPE_BIN" ]]; then
  echo "gamescope is required (install via distro package or PPA)." >&2
  exit 2
fi

RC_FILE="$(mktemp)"
cleanup() { rm -f "$RC_FILE"; }
trap cleanup EXIT

echo "Starting nested gamescope Deck/Bazzite emulation..."
# Deck LCD-ish nested size; SDL backend works on desktop NVIDIA/AMD hosts.
# SteamDeck=1 and SCB_* mirror Bazzite Game Mode advertising / ScopeBuddy guest mode.
    set +e
timeout -k 5 180 "$GAMESCOPE_BIN" \
  -W 1280 -H 800 -w 1280 -h 800 \
  --backend sdl \
  -- \
  env SteamDeck=1 SCB_GAMEMODE=1 SCB_NOSCOPE=1 OPENBOX_DECK_EMU_RC="$RC_FILE" \
  python3 "$ROOT/tests/test_gamescope_deck_emu.py"
gs_status=$?
set -e

if [[ -f "$RC_FILE" && -s "$RC_FILE" ]]; then
  rc="$(cat "$RC_FILE")"
  exit "$rc"
fi

# Fallback if the child never wrote a status (gamescope failed to start, etc.)
exit "$gs_status"
