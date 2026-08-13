#!/usr/bin/env python3
"""Stock theme packaging and install tests."""

import tempfile
import unittest
from pathlib import Path

from stock_themes import ensure_stock_themes, is_stock_theme, stock_theme_sources


ROOT = Path(__file__).parent


class StockThemesTests(unittest.TestCase):
    def test_bundled_sources_exist(self):
        sources = stock_theme_sources(ROOT)
        names = {path.stem for path in sources}
        self.assertGreaterEqual(len(sources), 5)
        for expected in (
            "Midnight Circuit",
            "Phosphor Terminal",
            "Harbor Light",
            "Cinema Marquee",
            "Nordic Mist",
        ):
            self.assertIn(expected, names)
            self.assertTrue(is_stock_theme(ROOT / "themes" / f"{expected}.css"))

    def test_ensure_installs_and_preserves_user_themes(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "themes"
            installed = ensure_stock_themes(destination, ROOT)
            self.assertEqual(len(installed), 5)
            custom = destination / "My Custom.css"
            custom.write_text("body { background: pink; }\n", encoding="utf-8")
            stock = destination / "Midnight Circuit.css"
            stock.write_text("/* OpenBox Stock Theme: Midnight Circuit */\nbody{}\n", encoding="utf-8")
            ensure_stock_themes(destination, ROOT)
            self.assertEqual(custom.read_text(encoding="utf-8"), "body { background: pink; }\n")
            self.assertTrue(is_stock_theme(stock))
            self.assertIn("Midnight Circuit", stock.read_text(encoding="utf-8"))

    def test_ensure_preserves_user_edits_to_stock_theme(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "themes"
            ensure_stock_themes(destination, ROOT)
            edited = destination / "Midnight Circuit.css"
            original = edited.read_bytes()
            edited.write_bytes(original + b"\n/* user tweak */\n")
            # Re-running must not revert the user's tweak.
            ensure_stock_themes(destination, ROOT)
            self.assertTrue(edited.read_bytes().endswith(b"/* user tweak */\n"))


if __name__ == "__main__":
    unittest.main()
