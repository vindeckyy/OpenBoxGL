#!/usr/bin/env python3
"""Platform capability contract (ADR 0086).

One server-computed feature object decides which platform-specific controls
exist. These tests pin the payload for all three platforms, prove markup and
frontend code only name declared features, and prove the legacy per-OS markers
are gone.
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg import platform_compat as pc  # noqa: E402
from pkg.platform_compat import platform_features  # noqa: E402

ALL_FEATURES = {
    "gamescope",
    "mangohud",
    "handheld_perf",
    "flatpak",
    "wine",
    "faugus",
    "lutris",
    "heroic",
    "epic",
    "native_storefronts",
    "desktop_entry",
}

# Settings the runtime applies only on Linux, and the feature that gates them.
LINUX_ONLY_SETTINGS = {
    "apply_perf": "handheld_perf",
    "gamescope_preset": "gamescope",
    "gamescope_custom_presets": "gamescope",
    "mangohud_enabled": "mangohud",
}

# The markup control for each Linux-only setting and the feature it must carry.
LINUX_ONLY_CONTROLS = (
    ('name="gamescope_preset"', "gamescope"),
    ('id="gamescopeCustomPresets"', "gamescope"),
    ('id="mangohudEnabled"', "mangohud"),
    ('id="applyPerf"', "handheld_perf"),
)


def features_for(*, linux=False, windows=False, macos=False):
    with mock.patch.object(pc, "IS_LINUX", linux), \
         mock.patch.object(pc, "IS_WINDOWS", windows), \
         mock.patch.object(pc, "IS_MACOS", macos):
        return platform_features()


class FeatureMatrixTests(unittest.TestCase):
    def test_linux_is_the_reference_channel(self):
        features = features_for(linux=True)
        self.assertEqual(set(features), ALL_FEATURES)
        self.assertEqual({key for key, value in features.items() if not value}, {"native_storefronts"})

    def test_windows_keeps_heroic_epic_and_native_storefronts(self):
        features = features_for(windows=True)
        self.assertEqual(
            {key for key, value in features.items() if value},
            {"heroic", "epic", "native_storefronts"},
        )

    def test_macos_has_no_launcher_sources_at_all(self):
        features = features_for(macos=True)
        self.assertFalse(any(features.values()))

    def test_every_value_is_a_bool(self):
        for platform in ({"linux": True}, {"windows": True}, {"macos": True}):
            for key, value in features_for(**platform).items():
                self.assertIsInstance(value, bool, key)


class MarkupTests(unittest.TestCase):
    def test_every_data_feature_names_a_declared_feature(self):
        markup = (ROOT / "index.html").read_text(encoding="utf-8")
        used = set(re.findall(r'data-feature="([a-z_]+)"', markup))
        self.assertTrue(used, "index.html declares no platform-gated controls")
        self.assertEqual(used - ALL_FEATURES, set())

    def test_legacy_platform_markers_are_gone(self):
        files = [ROOT / "index.html", *sorted((ROOT / "static").glob("*.js"))]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("data-macos-hidden", text, path.name)
            self.assertNotIn("data-windows-only", text, path.name)

    def test_windows_only_controls_start_hidden(self):
        markup = (ROOT / "index.html").read_text(encoding="utf-8")
        # Hidden-by-default keeps a failing control invisible during the
        # capabilities fetch; the feature attribute is what reveals it.
        for match in re.finditer(r'<[^>]*data-feature="native_storefronts"[^>]*>', markup):
            self.assertIn("hidden", match.group(0))

    def test_linux_only_settings_controls_carry_their_feature(self):
        markup = (ROOT / "index.html").read_text(encoding="utf-8")
        for needle, feature in LINUX_ONLY_CONTROLS:
            self.assertIn(needle, markup)
            start = markup.index(needle)
            # The control or the wrapper around it carries the marker.
            window = markup[max(0, start - 400):start + 400]
            self.assertIn(f'data-feature="{feature}"', window, needle)


class SettingsTests(unittest.TestCase):
    def test_linux_only_settings_map_to_declared_features(self):
        from settings_schema import KNOWN_SETTINGS

        for key, feature in LINUX_ONLY_SETTINGS.items():
            self.assertIn(key, KNOWN_SETTINGS)
            self.assertIn(feature, ALL_FEATURES)

    def test_each_mapped_feature_is_linux_only(self):
        linux = features_for(linux=True)
        windows = features_for(windows=True)
        macos = features_for(macos=True)
        for feature in set(LINUX_ONLY_SETTINGS.values()):
            self.assertTrue(linux[feature], feature)
            self.assertFalse(windows[feature], feature)
            self.assertFalse(macos[feature], feature)


class OpenerAuditTests(unittest.TestCase):
    def test_platform_openers_live_in_the_platform_module(self):
        # A stray opener on the wrong platform (defects 5, 14, 15) is how the
        # storefront catalog and Steam launch paths broke on Windows and macOS.
        forbidden = re.compile(r'os\.startfile\(|["\']/usr/bin/open["\']|shutil\.which\("xdg-open"\)')
        platform_module = ROOT / "pkg" / "platform_compat.py"
        files = [*sorted(ROOT.glob("*.py")), *sorted((ROOT / "handlers").glob("*.py")),
                 *sorted((ROOT / "pkg").rglob("*.py")), *sorted((ROOT / "routes").glob("*.py"))]
        offenders = [
            str(path.relative_to(ROOT))
            for path in files
            if path != platform_module and forbidden.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


class FrontendUsageTests(unittest.TestCase):
    def test_every_features_dot_literal_is_declared(self):
        used = set()
        for path in sorted((ROOT / "static").glob("*.js")):
            used |= set(re.findall(r"features\??\.([a-z_]+)", path.read_text(encoding="utf-8")))
        self.assertEqual(used - ALL_FEATURES, set())

    def test_setup_source_map_only_names_declared_features(self):
        setup = (ROOT / "static" / "setup.js").read_text(encoding="utf-8")
        match = re.search(r"const SOURCE_FEATURE = \{(.*?)\};", setup, re.S)
        self.assertIsNotNone(match, "setup.js lost its source-to-feature map")
        values = set(re.findall(r":\s*'([a-z_]+)'", match.group(1)))
        self.assertEqual(values - ALL_FEATURES, set())

    def test_visibility_reads_the_contract_not_the_platform(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        start = app.index("function applyPlatformVisibility")
        block = app[start:app.index("\n    }", start)]
        self.assertIn("[data-feature]", block)
        self.assertNotIn("nativeCaps.platform", block)

    def test_default_caps_carry_an_empty_feature_object(self):
        state = (ROOT / "static" / "state.js").read_text(encoding="utf-8")
        match = re.search(r"const nativeCaps = \{(.*?)\};", state, re.S)
        self.assertIsNotNone(match)
        self.assertIn("features:{}", match.group(1).replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
