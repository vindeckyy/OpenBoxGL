#!/usr/bin/env python3
"""Stock theme packaging, offline, contrast, and install tests."""

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / "themes").is_dir() and (ROOT.parent / "themes").is_dir():
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from stock_themes import ensure_stock_themes, is_stock_theme, stock_theme_sources  # noqa: E402

APP_CSS = ROOT / "static" / "app.css"
ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)
VAR_DEF_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}\b")


def _parse_root_vars(css_text: str):
    match = ROOT_BLOCK_RE.search(css_text)
    if not match:
        return {}
    return {name: value.strip() for name, value in VAR_DEF_RE.findall(match.group(0))}


def _hex_to_rgb(value: str):
    value = value.strip().lower()
    if not value.startswith("#"):
        return None
    if len(value) == 4:
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if len(value) != 7:
        return None
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _relative_luminance(rgb):
    channels = []
    for channel in rgb:
        scaled = channel / 255
        channels.append(scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    fg = _relative_luminance(_hex_to_rgb(fg_hex))
    bg = _relative_luminance(_hex_to_rgb(bg_hex))
    lighter = max(fg, bg)
    darker = min(fg, bg)
    return (lighter + 0.05) / (darker + 0.05)


def _resolve_color(token_map, name):
    value = token_map.get(name, "").strip()
    if value.startswith("var(--"):
        ref = value[6:value.index(")")].strip()
        return _resolve_color(token_map, ref)
    if value.startswith("#"):
        return value
    return None


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

    def test_ensure_refreshes_stale_stock_theme(self):
        """A stock theme from an older build keeps its edit-preserving
        guarantee only for the current version; stale stock must be replaced
        so shipped CSS fixes reach existing installs."""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "themes"
            destination.mkdir()
            stale = destination / "Cinema Marquee.css"
            stale.write_text(
                "/* OpenBox Stock Theme: Cinema Marquee */\n"
                ".topbar { backdrop-filter: blur(8px); }\n",
                encoding="utf-8",
            )
            installed = ensure_stock_themes(destination, ROOT)
            self.assertIn("Cinema Marquee", installed)
            refreshed = stale.read_text(encoding="utf-8")
            self.assertIn("v3", refreshed)
            self.assertNotIn("backdrop-filter: blur(8px)", refreshed)
            # A second run with the current version is a no-op.
            self.assertEqual(ensure_stock_themes(destination, ROOT), [])

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

    def test_stock_themes_are_offline_and_token_only(self):
        app_tokens = set(_parse_root_vars(APP_CSS.read_text()).keys())
        for path in stock_theme_sources(ROOT):
            css = path.read_text(encoding="utf-8")
            self.assertNotIn("http", css.lower(), f"{path.name} must not request remote assets")
            self.assertNotIn("@import", css, f"{path.name} must not import remote fonts")
            outside = ROOT_BLOCK_RE.sub("", css)
            self.assertEqual(len(HEX_RE.findall(outside)), 0, f"{path.name} must be token-only outside :root")
            theme_tokens = set(_parse_root_vars(css).keys())
            missing = sorted(app_tokens - theme_tokens)
            self.assertFalse(missing, f"{path.name} missing :root tokens: {missing[:8]}")

    def test_stock_themes_meet_wcag_aa_contrast(self):
        for path in stock_theme_sources(ROOT):
            tokens = _parse_root_vars(path.read_text(encoding="utf-8"))
            text = _resolve_color(tokens, "text")
            bg = _resolve_color(tokens, "bg")
            muted = _resolve_color(tokens, "muted")
            self.assertIsNotNone(text, f"{path.name} needs resolvable --text")
            self.assertIsNotNone(bg, f"{path.name} needs resolvable --bg")
            self.assertGreaterEqual(_contrast_ratio(text, bg), 4.5, f"{path.name} text/bg contrast")
            if muted:
                self.assertGreaterEqual(_contrast_ratio(muted, bg), 4.5, f"{path.name} muted/bg contrast")


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
