#!/usr/bin/env python3
"""Bulk literal find/replace stays scoped to selected games and one field."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: E402,F401
from pkg.parity.parity_bulk_edit import find_replace  # noqa: E402


def games():
    return [
        {"game_id": "g1", "name": "Super Mario", "genre": "Platformer"},
        {"game_id": "g2", "name": "Mario Kart", "genre": "Racing"},
        {"game_id": "g3", "name": "Zelda", "genre": "Adventure"},
    ]


class BulkEditTests(unittest.TestCase):
    def test_dry_run_reports_matches_without_writing(self):
        value = games()
        result = find_replace(value, ["g1", "g2"], "name", "Mario", "Mario Bros")
        self.assertEqual(result["matched"], 2)
        self.assertFalse(result["applied"])
        self.assertEqual(value[0]["name"], "Super Mario")

    def test_apply_scopes_to_selection_and_field(self):
        value = games()
        result = find_replace(value, ["g2"], "name", "Mario", "Mario Bros", dry_run=False)
        self.assertTrue(result["applied"])
        self.assertEqual(value[0]["name"], "Super Mario")
        self.assertEqual(value[1]["name"], "Mario Bros Kart")
        self.assertEqual(value[1]["genre"], "Racing")

    def test_unknown_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "field"):
            find_replace(games(), ["g1"], "path", "/", "x")

    def test_empty_selection_and_find_are_rejected(self):
        with self.assertRaises(ValueError):
            find_replace(games(), [], "name", "Mario", "x")
        with self.assertRaises(ValueError):
            find_replace(games(), ["g1"], "name", "", "x")

    def test_preview_is_bounded(self):
        value = [{"game_id": f"g{index}", "name": "Mario"} for index in range(150)]
        result = find_replace(value, [item["game_id"] for item in value], "name", "Mario", "Luigi")
        self.assertEqual(len(result["games"]), 100)
        self.assertEqual(result["truncated"], 50)


if __name__ == "__main__":
    unittest.main()
