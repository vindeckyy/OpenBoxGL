"""Tests for the gamescope preset launch wrap (pkg/state/launch.py)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.state.launch import _apply_gamescope_preset_from_state  # noqa: E402


class GamescopePresetWrapTest(unittest.TestCase):
    def test_empty_args_returned_unchanged(self):
        self.assertEqual(_apply_gamescope_preset_from_state({"settings": {}}, {}, []), [])

    def test_no_preset_returns_args_unchanged(self):
        args = ["emulator", "{path}"]
        self.assertEqual(_apply_gamescope_preset_from_state({"settings": {}}, {}, args), args)
        self.assertEqual(_apply_gamescope_preset_from_state({"settings": {"gamescope_preset": "  "}}, {}, args), args)

    def test_global_preset_wraps_args(self):
        state = {"settings": {"gamescope_preset": "1080p"}}
        with patch("pkg.state.launch.should_nest_gamescope", return_value=True):
            wrapped = _apply_gamescope_preset_from_state(state, {}, ["run", "game"])
        self.assertEqual(wrapped[0], "gamescope")
        self.assertEqual(wrapped[-1], "game")
        self.assertIn("--", wrapped)
        self.assertIn("1920", wrapped)

    def test_nesting_guard_blocks_wrap(self):
        args = ["run", "game"]
        with patch("pkg.state.launch.should_nest_gamescope", return_value=False):
            self.assertEqual(_apply_gamescope_preset_from_state({"settings": {"gamescope_preset": "deck"}}, {}, args), args)

    def test_unknown_preset_returns_args_unchanged(self):
        args = ["run", "game"]
        state = {"settings": {"gamescope_preset": "does-not-exist"}}
        with patch("pkg.state.launch.should_nest_gamescope", return_value=True):
            self.assertEqual(_apply_gamescope_preset_from_state(state, {}, args), args)

    def test_custom_preset_resolves_from_settings(self):
        state = {"settings": {
            "gamescope_preset": "Deck 90",
            "gamescope_custom_presets": [{"name": "Deck 90", "width": 1280, "height": 800, "refresh": 90}],
        }}
        with patch("pkg.state.launch.should_nest_gamescope", return_value=True):
            wrapped = _apply_gamescope_preset_from_state(state, {}, ["run"])
        self.assertEqual(wrapped[:7], ["gamescope", "-W", "1280", "-H", "800", "-r", "90"])
        self.assertEqual(wrapped[7], "--")

    def test_per_game_preset_wins_over_global(self):
        state = {"settings": {"gamescope_preset": "1080p"}}
        game = {"gamescope_preset": "borderless"}
        with patch("pkg.state.launch.should_nest_gamescope", return_value=True):
            wrapped = _apply_gamescope_preset_from_state(state, game, ["run"])
        self.assertEqual(wrapped, ["gamescope", "-b", "--", "run"])

    def test_per_game_empty_falls_back_to_global(self):
        state = {"settings": {"gamescope_preset": "borderless"}}
        with patch("pkg.state.launch.should_nest_gamescope", return_value=True):
            wrapped = _apply_gamescope_preset_from_state(state, {"gamescope_preset": ""}, ["run"])
        self.assertEqual(wrapped, ["gamescope", "-b", "--", "run"])

    def test_non_dict_game_uses_global_only(self):
        state = {"settings": {"gamescope_preset": "borderless"}}
        with patch("pkg.state.launch.should_nest_gamescope", return_value=True):
            wrapped = _apply_gamescope_preset_from_state(state, None, ["run"])
        self.assertEqual(wrapped, ["gamescope", "-b", "--", "run"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
