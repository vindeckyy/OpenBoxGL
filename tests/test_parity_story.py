#!/usr/bin/env python3
"""Tests for the per-game story projection (1.12.0)."""

import contextlib
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_story import game_story  # noqa: E402


def _require_tzset():
    if not hasattr(time, "tzset"):
        raise unittest.SkipTest("requires POSIX time.tzset")


@contextlib.contextmanager
def _fixed_tz(name):
    """Pin the process local timezone for the test; restored on exit."""
    _require_tzset()
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def _local_day_key(iso_value):
    """Local day key with full ISO semantics: aware -> local, naive -> local.

    Mirrors the story-view frontend contract (static/library.js localDayKey):
    a late-night aware moment must group with the naive session of the same
    local day, not with the UTC date its raw string prefix suggests.
    """
    text = str(iso_value or "")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return text[:10]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date().isoformat()


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


def _tz_state():
    """Mixed aware + naive timestamps sharing one local day.

    The moment is 23:30 local (EDT), stored the way handlers/moments.py
    stores it — UTC-aware, i.e. 03:30 UTC the next day. The 23:00 session
    is a naive local stamp. Raw UTC slicing would file the moment under
    2026-09-22 while the session sits on 2026-09-21.
    """
    return {
        "games": [
            {
                "game_id": "g-tz", "name": "TZ Quest", "platform": "PC",
                "added_at": "2026-09-01T10:00:00",
                "moments": [
                    {"moment_id": "m-tz", "title": "Late night frag",
                     "created_at": "2026-09-22T03:30:00+00:00"},
                ],
            },
        ],
        "history": [
            {"game_id": "g-tz", "game": "TZ Quest", "started": "2026-09-20T10:00:00", "seconds": 600},
            {"game_id": "g-tz", "game": "TZ Quest", "started": "2026-09-21T23:00:00", "seconds": 1200},
        ],
    }


def test_mixed_aware_naive_ordering():
    """An aware moment sorts by its true instant against naive sessions."""
    with _fixed_tz("America/New_York"):  # EDT (-04:00) in September
        story = game_story(_tz_state(), _tz_state()["games"][0])
    kinds = [event["kind"] for event in story["events"]]
    # added < first_played (09-20) < longest session (09-21 23:00 naive,
    # 03:00Z) < moment (09-21 23:30 EDT stored as 09-22 03:30Z).
    assert kinds == ["added", "first_played", "session", "moment"], kinds
    stamps = [event["at"] for event in story["events"]]
    assert stamps == sorted(stamps)
    assert story["totals"]["sessions"] == 2


def test_mixed_aware_naive_share_local_day():
    """A late-night aware moment groups with the naive session of the same local day."""
    with _fixed_tz("America/New_York"):  # EDT (-04:00) in September
        story = game_story(_tz_state(), _tz_state()["games"][0])
        # Contract vectors shared with the frontend localDayKey: the moment's
        # stored UTC instant and the naive session both key to 2026-09-21.
        assert _local_day_key("2026-09-22T03:30:00+00:00") == "2026-09-21"
        assert _local_day_key("2026-09-21T23:30:00-04:00") == "2026-09-21"
        assert _local_day_key("2026-09-21T23:00:00") == "2026-09-21"
        moment_days = [_local_day_key(e["at"]) for e in story["events"] if e["kind"] == "moment"]
        session_days = [_local_day_key(e["at"]) for e in story["events"] if e["kind"] == "session"]
        assert moment_days == session_days == ["2026-09-21"]


if __name__ == "__main__":
    skipped = 0
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        try:
            fn()
        except unittest.SkipTest as exc:
            skipped += 1
            print(f"skip {name}: {exc}")
            continue
        print(f"ok {name}")
    print(f"all tests passed ({skipped} skipped)")
