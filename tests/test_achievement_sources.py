#!/usr/bin/env python3
"""Achievement source projection and manual corrections (P11.2/P11.3)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.state.cache import _achievement_source, _achievement_sources  # noqa: E402
from retroachievements import game_progress  # noqa: E402


def test_empty_sources_are_omitted():
    assert _achievement_source("Nothing", 0, 0) is None
    assert _achievement_source("Zero", 0, 0) is None
    source = _achievement_source("Steam", 3, 5)
    assert source["progress_pct"] == 60.0
    assert source["mastered"] is False
    assert _achievement_source("Done", 5, 5)["mastered"] is True


def test_sources_merge_plugin_and_manual_override():
    game = {"game_id": "g1", "ra_game_id": "42", "steam_app_id": ""}
    ra_cache = {"earned": 2, "total": 4, "achievements": [{"title": "A"}]}
    plugin_entry = {"sources": {
        "plugin:vendor:sets": {"name": "Vendor", "earned": 1, "total": 3},
    }}
    overrides = {"g1": {
        "plugin:vendor:sets": {"earned": 2, "total": 3, "manual": True},
        "steam": {"earned": 1, "total": 1, "manual": True},
    }}
    sources = _achievement_sources(game, ra_cache, Path("/nonexistent"), plugin_entry, overrides)
    assert sources["retroachievements"]["earned"] == 2
    assert sources["retroachievements"]["total"] == 4
    assert sources["plugin:vendor:sets"]["earned"] == 2
    assert sources["plugin:vendor:sets"]["manual"] is True
    assert sources["steam"]["total"] == 1
    assert sources["steam"]["manual"] is True


def test_zero_total_override_removes_a_source():
    game = {"game_id": "g1", "ra_game_id": "42", "steam_app_id": ""}
    ra_cache = {"earned": 2, "total": 4, "achievements": []}
    sources = _achievement_sources(
        game, ra_cache, Path("/nonexistent"), {},
        {"g1": {"retroachievements": {"earned": 0, "total": 0, "manual": True}}},
    )
    assert "retroachievements" not in sources


def test_retroachievements_progress_reports_beaten_and_mastered():
    payload = {
        "Title": "Game",
        "NumAwardedToUser": 4,
        "NumAwardedToUserHardcore": 6,
        "NumAchievements": 6,
        "Beaten": 1,
        "UserCompletion": "100%",
        "Achievements": {},
    }
    progress = game_progress(1, {"username": "u", "api_key": "k"}, fetch=lambda *args: payload)
    assert progress["beaten"] == 1
    assert progress["mastered"] is True
    assert progress["earned_hardcore"] == 6


if __name__ == "__main__":
    test_empty_sources_are_omitted()
    test_sources_merge_plugin_and_manual_override()
    test_zero_total_override_removes_a_source()
    test_retroachievements_progress_reports_beaten_and_mastered()
    print("achievement sources self-test: ok")
