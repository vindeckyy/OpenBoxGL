#!/usr/bin/env python3
"""Ensure screenshot/demo fixtures never remain in user libraries."""

import os
import tempfile

# Isolate from the real data dir before importing openbox.
# noqa: E402 - the environment must be set before openbox resolves DATA.
_ISOLATED = tempfile.mkdtemp(prefix="openbox-demo-purge-")
os.environ["OPENBOX_DATA_DIR"] = _ISOLATED

from openbox import is_demo_game, purge_demo_games  # noqa: E402


def main():
    state = {
        "games": [
            {"name": "Real Game", "path": "/games/real.bin"},
            {"name": "Chrono Trigger", "path": "/tmp/openbox-screenshots/roms/ChronoTrigger.smc"},
            {"name": "Flagged demo", "path": "/games/other.bin", "demo": True},
        ],
        "profiles": {},
        "history": [],
    }
    assert is_demo_game(state["games"][1])
    assert is_demo_game(state["games"][2])
    assert not is_demo_game(state["games"][0])
    removed = purge_demo_games(state)
    assert removed == 2
    assert len(state["games"]) == 1
    assert state["games"][0]["name"] == "Real Game"
    print("demo purge self-test: ok")


if __name__ == "__main__":
    main()
