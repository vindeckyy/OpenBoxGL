#!/usr/bin/env python3
"""Tests for the per-game story projection (1.12.0)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_story import game_story  # noqa: E402


def _state():
    return {
        "games": [
            {
                "game_id": "g-1", "name": "Quake", "platform": "PC",
                "added_at": "2026-01-10T10:00:00", "progress": "Beaten",
                "playtime_seconds": 7200, "last_played": "2026-02-01T20:00:00",
                "play_count": 2,
                "moments": [
                    {"moment_id": "m-1", "title": "First frag", "note": "nice",
                     "created_at": "2026-01-15T21:00:00"},
                ],
            },
        ],
        "history": [
            {"game_id": "g-1", "game": "Quake", "started": "2026-01-12T18:00:00", "seconds": 3600},
            {"game_id": "g-1", "game": "Quake", "started": "2026-02-01T19:00:00", "seconds": 3600},
            {"game_id": "other", "game": "Doom", "started": "2026-01-20T10:00:00", "seconds": 600},
        ],
    }


def test_events_ordered_and_complete():
    story = game_story(_state(), _state()["games"][0])
    kinds = [event["kind"] for event in story["events"]]
    assert kinds[0] == "added"
    assert "first_played" in kinds
    assert "moment" in kinds
    assert "progress" in kinds
    # timestamps are non-decreasing
    stamps = [event["at"] for event in story["events"]]
    assert stamps == sorted(stamps)
    assert story["totals"]["sessions"] == 2
    assert story["totals"]["playtime_seconds"] == 7200


def test_milestones_from_journal():
    story = game_story(_state(), _state()["games"][0])
    milestones = [e for e in story["events"] if e["kind"] == "milestone"]
    # 7200s total crosses the 1h mark via journal; 5h not reached.
    assert any(e["title"] == "1h played" for e in milestones)
    assert not any(e["title"] == "5h played" for e in milestones)


def test_legacy_history_fallback_by_name():
    state = _state()
    for row in state["history"]:
        row.pop("game_id", None)
    story = game_story(state, state["games"][0])
    assert story["totals"]["sessions"] == 2  # Doom row excluded by name


def test_keyed_rows_do_not_leak_into_unkeyed_game():
    """A game record without game_id must not claim rows that carry one.

    Regression: the old match skipped the id check when the game had no id,
    so any keyed row (even a same-named other game) leaked into the story.
    """
    state = _state()
    game = {"name": "Quake", "platform": "PC"}  # no game_id
    story = game_story(state, game)
    assert story["totals"]["sessions"] == 0


def test_empty_game():
    story = game_story({"games": [], "history": []}, {"game_id": "x", "name": "Ghost"})
    assert story["events"] == []
    assert story["totals"]["sessions"] == 0


def test_imported_playtime_milestone():
    state = _state()
    state["history"] = []  # playtime came from an import, not the journal
    story = game_story(state, state["games"][0])
    assert any(e["kind"] == "milestone" and e["title"] == "1h played" for e in story["events"])


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        fn()
        print(f"ok {name}")
    print("all tests passed")
