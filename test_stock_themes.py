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

    def test_stock_themes_keep_tools_menu_usable(self):
        """The Tools menu is position:fixed inside the topbar; a theme must not
        turn .topbar into its containing block (backdrop-filter) or push the
        fixed overlays into normal flow (position:relative), or the menu stops
        opening after a theme is applied."""
        for path in stock_theme_sources(ROOT):
            css = path.read_text(encoding="utf-8")
            for block in _css_blocks(css):
                selector, body = block
                if ".topbar" in selector.split("::")[0].replace(" ", ""):
                    self.assertNotIn(
                        "backdrop-filter", body,
                        f"{path.name}: backdrop-filter on .topbar clips the fixed Tools menu",
                    )
                if "position: relative" in body or "position:relative" in body:
                    for overlay in (".lifecycle", ".bigbox", ".toast", ".tool-menu", "dialog"):
                        if overlay in selector:
                            self.fail(
                                f"{path.name}: {overlay} must not be forced to position:relative"
                            )


def _css_blocks(css):
    """Yield (selector, body) pairs for each top-level {...} block."""
    depth = 0
    start = None
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                selector = css[:start].rsplit("}", 1)[-1].strip()
                yield selector, css[start + 1:index]
                start = None


if __name__ == "__main__":
    unittest.main()
