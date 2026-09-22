from catalog import bulk_update, related_game_ids
from catalog import (canon_progress, clean_user_rating, display_progress,
                     normalize_manual_sessions, normalize_notes,
                     progress_suggest_due, total_playtime_seconds)


def main():
    games = [
        {"game_id": "game-a", "name": "Alpha", "platform": "NES", "genre": "Action Adventure", "series": "Saga"},
        {"game_id": "game-b", "name": "Beta", "platform": "NES", "genre": "Action", "series": "Saga"},
        {"game_id": "game-c", "name": "Gamma", "platform": "PC", "genre": "Strategy"},
        {"game_id": "game-d", "name": "Hidden", "platform": "NES", "genre": "Action", "series": "Saga", "hidden": True},
    ]
    assert related_game_ids(games, 0) == [1]
    # Mixed stable ids and integer indexes must not raise TypeError.
    assert bulk_update(games, ["game-b", 1], {"progress": "Completed", "rating": 4.5, "favorite": True}) == 1
    assert games[1]["rating"] == 4.5 and games[1]["progress"] == "Completed"
    try:
        bulk_update(games, [0], {"rating": 6})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid rating accepted")
    print("catalog self-test: ok")


def _f4_backlog_checks():
    # F4a: "Unplayed" is a write/display alias; storage stays "".
    assert canon_progress("Unplayed") == "" and canon_progress("unplayed") == ""
    assert canon_progress("Playing") == "Playing"
    assert display_progress("") == "Unplayed" and display_progress("Playing") == "Playing"
    # F4b: personal star rating is int 0-5, distinct from metadata rating.
    assert clean_user_rating(0) == 0 and clean_user_rating(5) == 5
    assert clean_user_rating("3") == 3
    for bad in (-1, 6, "many", None):
        try:
            clean_user_rating(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad user_rating accepted: {bad!r}")
    # F4d: legacy string notes migrate to one dated entry on read.
    assert normalize_notes(None) == []
    assert normalize_notes("old note") == [{"ts": "", "text": "old note"}]
    assert normalize_notes([{"text": "a"}, {"ts": "2026-01-01T00:00:00", "text": "b"}]) == [
        {"ts": "", "text": "a"}, {"ts": "2026-01-01T00:00:00", "text": "b"}]
    assert normalize_notes([{"text": "   "}]) == []
    # F4c: manual sessions normalize; totals add observed + manual.
    sessions = normalize_manual_sessions([
        {"date": "2026-01-01", "seconds": 3600, "note": "x"},
        {"date": "nope", "seconds": "not a number"},
        {"date": "2026-01-02", "seconds": -5},
        {"seconds": 60},
    ])
    assert sessions == [
        {"date": "2026-01-01", "seconds": 3600, "note": "x"},
        {"date": "", "seconds": 60, "note": ""},
    ]
    assert total_playtime_seconds({"playtime_seconds": 100, "manual_playtime_seconds": 3660}) == 3760
    assert total_playtime_seconds({}) == 0
    # F4a: one-time auto-suggest eligibility. The launch automation defaults
    # to "Playing" when the setting is absent (see _make_start_mutator), so
    # the suggest only fires when the user explicitly disabled it.
    game = {"progress": ""}
    assert progress_suggest_due(game, {"backlog_progress_suggest": True, "progress_on_first_play": ""}) is True
    assert progress_suggest_due({"progress": "", "progress_suggested": True}, {"backlog_progress_suggest": True, "progress_on_first_play": ""}) is False
    assert progress_suggest_due({"progress": "Playing"}, {"backlog_progress_suggest": True, "progress_on_first_play": ""}) is False
    assert progress_suggest_due(game, {"backlog_progress_suggest": False}) is False
    assert progress_suggest_due(game, {"backlog_progress_suggest": True}) is False  # default "Playing" automation active
    assert progress_suggest_due(game, {"backlog_progress_suggest": True, "progress_on_first_play": "Playing"}) is False
    # Bulk accepts the Unplayed alias and user_rating.
    games = [{"game_id": "g1", "name": "A"}]
    assert bulk_update(games, [0], {"progress": "Unplayed", "user_rating": 4}) == 1
    assert games[0]["progress"] == "" and games[0]["user_rating"] == 4
    try:
        bulk_update(games, [0], {"user_rating": 9})
    except ValueError:
        pass
    else:
        raise AssertionError("bad bulk user_rating accepted")
    print("catalog f4 backlog checks: ok")


if __name__ == "__main__":
    main()
    _f4_backlog_checks()
