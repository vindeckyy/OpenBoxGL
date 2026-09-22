"""F4a: one-time launch auto-suggest wiring in pkg/state/launch.py."""
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("OPENBOX_DATA_DIR", tempfile.mkdtemp())

import pkg.parity  # noqa: F401,E402  # register flat-import finder
from pkg.state import launch as launch_mod  # noqa: E402


def _mutator(entry):
    return launch_mod._make_start_mutator(
        "gid-1", 0, datetime(2026, 9, 22, 12, 0, 0),
        MagicMock(pid=1234), entry, False, "launch-1", None)


def main():
    # Suggest fires once for an unplayed game when the user turned the
    # first-play automation off ("None") and left the kill switch on.
    entry = {}
    state = {"games": [{"game_id": "gid-1", "name": "G", "progress": ""}],
             "settings": {"backlog_progress_suggest": True, "progress_on_first_play": ""}}
    _mutator(entry)(state)
    assert entry["progress_suggest"] is True, entry
    assert state["games"][0]["progress_suggested"] is True

    # Prompted-once persistence: the flag survives, the suggest never repeats.
    entry2 = {}
    _mutator(entry2)(state)
    assert entry2["progress_suggest"] is False, entry2

    # Kill switch off: no suggest, no flag.
    entry3 = {}
    state3 = {"games": [{"game_id": "gid-1", "name": "G", "progress": ""}],
              "settings": {"backlog_progress_suggest": False, "progress_on_first_play": ""}}
    _mutator(entry3)(state3)
    assert entry3["progress_suggest"] is False, entry3
    assert "progress_suggested" not in state3["games"][0]

    # Default automation (progress_on_first_play unset -> "Playing") sets the
    # status itself, so the suggest stays quiet.
    entry4 = {}
    state4 = {"games": [{"game_id": "gid-1", "name": "G", "progress": ""}],
              "settings": {"backlog_progress_suggest": True}}
    _mutator(entry4)(state4)
    assert state4["games"][0]["progress"] == "Playing"
    assert entry4["progress_suggest"] is False, entry4

    # Explicit progress_on_first_play automation takes precedence too.
    entry5 = {}
    state5 = {"games": [{"game_id": "gid-1", "name": "G", "progress": ""}],
              "settings": {"backlog_progress_suggest": True, "progress_on_first_play": "Paused"}}
    _mutator(entry5)(state5)
    assert state5["games"][0]["progress"] == "Paused"
    assert entry5["progress_suggest"] is False, entry5

    print("backlog launch suggest: ok")


if __name__ == "__main__":
    main()
