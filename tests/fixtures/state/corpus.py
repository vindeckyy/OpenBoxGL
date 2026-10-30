"""Deterministic v6 library corpus for the migration tests (plan W1-07).

``make_v6_library(count)`` is the one generator behind the 100 and 10,000 game migration
cases. ``v6/hundred.json`` is the committed 100-game copy; ``test_state_v7`` fails when the
two drift apart. Regenerate it after an intentional change with::

    python3 -B tests/fixtures/state/corpus.py
"""

from __future__ import annotations

import json
from pathlib import Path

PLATFORMS = ("PS1", "PS2", "PC", "GBA", "SNES")
HUNDRED = Path(__file__).resolve().parent / "v6" / "hundred.json"


def make_game(index: int) -> dict:
    platform = PLATFORMS[index % len(PLATFORMS)]
    return {
        "game_id": f"game-{index:05d}",
        "name": f"Game {index}",
        "platform": platform,
        "path": f"/roms/{platform.lower()}/game-{index}.iso",
        "rating": index % 5,
        "playtime_seconds": index * 7,
    }


def make_v6_library(count: int) -> dict:
    return {
        "schema_version": 6,
        "games": [make_game(index) for index in range(count)],
        "profiles": {},
        "history": [],
        "settings": {"theme": "dark"},
        "playlists": [],
        "queue": [],
        "notifications": [],
        "ui_state": {},
        "active_sessions": [],
        "trash": [],
        "party_decks": [],
        "emulator_settings": {},
        "achievement_overrides": {},
    }


if __name__ == "__main__":
    HUNDRED.write_text(json.dumps(make_v6_library(100), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {HUNDRED}")
