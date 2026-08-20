#!/usr/bin/env python3
"""Frontend contract and asset routing tests."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / "static").is_dir() and (ROOT.parent / "static").is_dir():
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from routes import PUBLIC_GET_PATHS  # noqa: E402

EXPECTED_PEER_MODULES = frozenset({
    "util.js",
    "state.js",
    "library.js",
    "settings.js",
    "imports.js",
    "metadata.js",
    "media.js",
    "reader.js",
    "sessions.js",
    "storefront.js",
    "bigbox.js",
    "dialogs.js",
})


class FrontendContractTests(unittest.TestCase):
    def test_index_html_loads_app_js_module(self):
        index_path = ROOT / "index.html"
        self.assertTrue(index_path.is_file(), f"index.html must exist at {index_path}")
        content = index_path.read_text(encoding="utf-8")

        # Must include the module entry script
        self.assertRegex(
            content,
            r'<script\s+type=["\']module["\']\s+src=["\']/static/app\.js["\']\s*>\s*</script>',
            "index.html must contain <script type=\"module\" src=\"/static/app.js\"></script>",
        )

        # Must link app stylesheet
        self.assertIn(
            '/static/app.css',
            content,
            "index.html must link /static/app.css",
        )

    def test_app_js_imports_all_peer_modules(self):
        app_js_path = ROOT / "static" / "app.js"
        self.assertTrue(app_js_path.is_file(), f"app.js must exist at {app_js_path}")
        content = app_js_path.read_text(encoding="utf-8")

        imported_modules = set(
            re.findall(r"from\s+['\"]\./([a-zA-Z0-9_-]+\.js)['\"]", content)
        )

        # Check all 12 peer modules are imported
        missing = EXPECTED_PEER_MODULES - imported_modules
        self.assertEqual(
            missing,
            set(),
            f"static/app.js is missing imports for peer modules: {missing}",
        )
        self.assertGreaterEqual(len(imported_modules), 12)

        # Assert every imported peer exists on disk
        for module_name in EXPECTED_PEER_MODULES:
            peer_path = ROOT / "static" / module_name
            self.assertTrue(
                peer_path.is_file(),
                f"Peer module {module_name} must exist on disk at {peer_path}",
            )

    def test_public_get_paths_covers_all_static_assets(self):
        # Base HTML routes
        self.assertIn("/", PUBLIC_GET_PATHS)
        self.assertIn("/index.html", PUBLIC_GET_PATHS)

        # CSS and app.js
        self.assertIn("/static/app.css", PUBLIC_GET_PATHS)
        self.assertIn("/static/app.js", PUBLIC_GET_PATHS)

        # All 13 static JS files must be reachable before authentication
        static_js_files = sorted((ROOT / "static").glob("*.js"))
        self.assertGreaterEqual(len(static_js_files), 13)

        for js_file in static_js_files:
            route_path = f"/static/{js_file.name}"
            self.assertIn(
                route_path,
                PUBLIC_GET_PATHS,
                f"PUBLIC_GET_PATHS must include {route_path}",
            )

    def test_util_and_state_exported_symbols(self):
        util_js = (ROOT / "static" / "util.js").read_text(encoding="utf-8")
        for symbol in (
            "$",
            "escapeHtml",
            "sortGames",
            "gameInstalled",
            "badge",
            "advancedQueryMatches",
            "parseQueryTokens",
        ):
            self.assertIn(
                symbol,
                util_js,
                f"static/util.js must define and export {symbol}",
            )

        state_js = (ROOT / "static" / "state.js").read_text(encoding="utf-8")
        for symbol in ("AppState", "api", "filteredGames"):
            self.assertIn(
                symbol,
                state_js,
                f"static/state.js must define and export {symbol}",
            )

    def test_eslint_config_exists(self):
        config = ROOT / "static" / "eslint.config.mjs"
        self.assertTrue(config.is_file(), "static/eslint.config.mjs must exist")
        lines = config.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(lines), 20)


if __name__ == "__main__":
    unittest.main()
