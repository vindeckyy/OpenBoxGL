#!/usr/bin/env python3
"""Shelf entries retain intentional non-playable semantics in projections."""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402
from pkg.parity.parity_export import build_export_games  # noqa: E402
from pkg.parity.parity_launch_doctor import run_preflight_checks  # noqa: E402
from pkg.state.cache import _filter_plugin_storefront_games, _project_game  # noqa: E402


def test_projection_marks_shelf_without_calling_it_broken():
    game = {"game_id": "shelf-1", "name": "Board Game", "manual_entry": True, "path": ""}
    result = _project_game(game, 0, set(), set(), [], {}, 0)
    assert result["manual_entry"] is True
    assert result["entry_type"] == "shelf"
    assert result["playable"] is False
    assert result["path_exists"] is False


def test_disabled_storefront_plugin_games_are_hidden():
    from unittest import mock

    games = [
        {"game_id": "plugin:store.one:1", "name": "One", "plugin_source": "store.one"},
        {"game_id": "plugin:store.two:2", "name": "Two", "plugin_source": "store.two"},
        {"game_id": "local", "name": "Local"},
    ]
    manifests = [
        {"id": "store.one", "valid": True, "enabled": True},
        {"id": "store.two", "valid": True, "enabled": False},
    ]
    with mock.patch("plugins.list_plugins", return_value=manifests):
        visible = _filter_plugin_storefront_games(games, Path(tempfile.mkdtemp()))
    assert [game["name"] for game in visible] == ["One", "Local"]


def test_export_and_doctor_preserve_shelf_semantics():
    game = {"game_id": "shelf-1", "name": "Board Game", "manual_entry": True, "path": ""}
    exported = build_export_games({"games": [copy.deepcopy(game)]})
    assert exported[0]["manual_entry"] is True
    checks = run_preflight_checks(game, {}, Path(tempfile.mkdtemp()))
    assert checks[0]["code"] == "SHELF_ENTRY"
    assert checks[0]["severity"] == "info"


if __name__ == "__main__":
    test_projection_marks_shelf_without_calling_it_broken()
    test_disabled_storefront_plugin_games_are_hidden()
    test_export_and_doctor_preserve_shelf_semantics()
    print("shelf projection tests: ok")
