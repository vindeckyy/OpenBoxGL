"""Tests for the ADR 0060 feature-regression ratchets.

Each gate is verified three ways: it passes on the real tree, it fails on a
simulated regression, and it fails on the bypass where the baseline itself is
edited to hide the regression. A gate that only passes is not evidence.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / "scripts"


def _load(name):
    """Import a gate script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(f"gate_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RouteContractTest(unittest.TestCase):
    """The HTTP surface may grow freely and shrink only on purpose."""

    def setUp(self):
        self.gate = _load("check_routes_contract")
        self.baseline = json.loads(self.gate.BASELINE.read_text(encoding="utf-8"))

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_baseline_covers_the_live_surface(self):
        live = self.gate.live_routes()
        for path in {entry["path"] for entry in self.baseline["routes"]}:
            self.assertIn(path, live, f"baselined route {path} is not registered")

    def test_baseline_matches_live_exactly(self):
        live = self.gate.live_routes()
        baselined = {entry["path"]: sorted(entry["methods"]) for entry in self.baseline["routes"]}
        self.assertEqual(baselined, live)

    def test_every_baseline_entry_has_known_methods(self):
        for entry in self.baseline["routes"]:
            self.assertTrue(entry["methods"], f"{entry['path']} has no methods")
            self.assertTrue(set(entry["methods"]) <= {"GET", "POST"}, entry["path"])



class SettingsContractTest(unittest.TestCase):
    """KNOWN_SETTINGS is destructive: dropping a key erases user data."""

    def setUp(self):
        self.gate = _load("check_settings_contract")

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_live_keys_match_the_baseline(self):
        baseline = set(self.gate.load_baseline()["keys"])
        self.assertEqual(baseline, self.gate.live_keys())

    def test_user_data_keys_are_known_to_the_gate(self):
        # Every key the gate protects as user data must actually be live,
        # otherwise the protection list is stale and misleading.
        live = self.gate.live_keys()
        for key in self.gate.USER_DATA_KEYS:
            self.assertIn(key, live, f"{key} is listed as user data but is not a live setting")

    def test_contract_file_is_valid(self):
        data = self.gate.load_baseline()
        self.assertIsNotNone(data)
        self.assertGreater(len(data["keys"]), 50)

    def test_removed_key_is_detected(self):
        # The gate's core promise, asserted against its own logic.
        baseline = set(self.gate.load_baseline()["keys"])
        live = self.gate.live_keys()
        self.assertEqual(set(), baseline - live)


class EmulatorDefsTest(unittest.TestCase):
    """Every definition must be complete, or it fails on Windows only."""

    def setUp(self):
        self.gate = _load("check_emulator_defs")

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_every_definition_is_complete(self):
        found, problems = self.gate.inspect()
        self.assertEqual([], problems)
        self.assertGreaterEqual(len(found), 24)


class FrontendModulesTest(unittest.TestCase):
    """The module graph must resolve and the shipped set must not shrink."""

    def setUp(self):
        self.gate = _load("check_frontend_modules")

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_graph_has_no_problems(self):
        self.assertEqual([], self.gate.check_graph())

    def test_resolve_rejects_missing_file(self):
        self.assertIsNone(self.gate.resolve("definitely_not_a_module.js"))

    def test_resolve_accepts_shipped_module(self):
        self.assertEqual("state.js", self.gate.resolve("state.js"))
        self.assertEqual("state.js", self.gate.resolve("state"))

    def test_entry_module_is_index_referenced(self):
        self.assertIn("app.js", (ROOT / "index.html").read_text(encoding="utf-8"))

    def test_baseline_matches_disk(self):
        baseline = set(json.loads(self.gate.BASELINE.read_text(encoding="utf-8"))["modules"])
        self.assertEqual(baseline, self.gate.shipped_modules())


class ReliabilityCatalogTest(unittest.TestCase):
    """A 'Tested' row must name a test that exists."""

    def setUp(self):
        self.gate = _load("check_reliability_catalog")

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_no_problems(self):
        self.assertEqual([], self.gate.check())

    def test_rows_are_contiguous(self):
        numbers = [number for number, *_ in self.gate.parse_rows()]
        self.assertEqual(list(range(1, len(numbers) + 1)), numbers)

    def test_every_tested_row_names_a_real_test(self):
        for number, scenario, _expected, status in self.gate.parse_rows():
            if not status.startswith("Tested"):
                continue
            refs = self.gate.TEST_REF_RE.findall(status)
            names_ui_smoke = self.gate.UI_SMOKE_REF in status
            self.assertTrue(
                refs or names_ui_smoke,
                f"row {number} ({scenario}) claims Tested with no test file",
            )
            if names_ui_smoke:
                self.assertTrue((ROOT / self.gate.UI_SMOKE_FILE).is_file())
            for _prefix, filename in refs:
                self.assertTrue((ROOT / "tests" / filename).is_file(), filename)

    def test_statuses_are_recognised(self):
        for number, scenario, _expected, status in self.gate.parse_rows():
            self.assertTrue(
                any(status.startswith(word) for word in self.gate.KNOWN_STATUSES),
                f"row {number} ({scenario}) has status {status!r}",
            )


class ClockCouplingTest(unittest.TestCase):
    """Calendar-pinned tests must be marked or fixed."""

    def setUp(self):
        self.gate = _load("check_clock_coupling")

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_no_unmarked_coupling(self):
        self.assertEqual([], self.gate.scan())

    def test_radio_test_is_explicitly_exempt(self):
        # The one file that broke on a date (commit 71d75ec) must stay visible.
        data = json.loads(self.gate.CONTRACT.read_text(encoding="utf-8"))
        self.assertIn("test_parity_radio.py", data["exempt"])
        text = (ROOT / "tests" / "test_parity_radio.py").read_text(encoding="utf-8")
        self.assertIn("clock-coupled", text)

    def test_rolling_windows_are_discovered(self):
        windows = self.gate._runtime_windows()
        self.assertIn("parity_radio", windows)
        self.assertTrue(any("RADIO_STALE_DAYS" in c for c in windows["parity_radio"]))

    def test_marker_detection(self):
        self.assertTrue(self.gate.MARKER_RE.search("# clock-coupled: reason"))
        self.assertFalse(self.gate.MARKER_RE.search("# nothing here"))


class ContractFilesTest(unittest.TestCase):
    """The frozen contracts must exist, stay well-formed, and stay wired in."""

    GATES = (
        "check_routes_contract.py",
        "check_settings_contract.py",
        "check_emulator_defs.py",
        "check_frontend_modules.py",
        "check_reliability_catalog.py",
        "check_clock_coupling.py",
    )
    CONTRACTS = (
        "routes.json",
        "settings.json",
        "emulator_defs.json",
        "frontend_modules.json",
        "reliability.json",
        "clock_coupling.json",
    )

    def test_every_contract_file_exists_and_parses(self):
        for name in self.CONTRACTS:
            path = SCRIPTS / "contracts" / name
            self.assertTrue(path.is_file(), f"missing contract {name}")
            json.loads(path.read_text(encoding="utf-8"))

    def test_gates_are_wired_into_the_orchestrator(self):
        text = (SCRIPTS / "check_tests.py").read_text(encoding="utf-8")
        for script in self.GATES:
            self.assertIn(script, text, f"{script} is not run by check_tests.py")

    def test_gates_are_wired_into_ci(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for script in self.GATES:
            self.assertIn(script, text, f"{script} is not run in CI")

    def test_locales_carry_no_hardcoded_version(self):
        import re

        pattern = re.compile(r"OpenBox\s+1\.\d+")
        for path in sorted((ROOT / "locales").glob("*.json")):
            self.assertIsNone(
                pattern.search(path.read_text(encoding="utf-8")),
                f"{path.name} hardcodes a version string",
            )

    def test_whatsnew_title_uses_a_placeholder(self):
        data = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        self.assertIn("{version}", data["whats_new"]["title"])

    def test_i18n_gate_sees_keys_held_in_string_arrays(self):
        """A key passed to t() through a variable is invisible to the call regex.

        whatsnew.js holds its rotating tips in a TIPS array and renders them with
        t(tipKey), so without the key-array scan a tip could be renamed and
        dropped from every locale without the i18n gate noticing.
        """
        import importlib.util
        import re

        spec = importlib.util.spec_from_file_location("gi18n", SCRIPTS / "check_i18n.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        found = checker._extract_js_keys()
        source = (ROOT / "static" / "whatsnew.js").read_text(encoding="utf-8")
        tips = set(re.findall(r"whats_new\.tip_\w+", source))
        self.assertTrue(tips, "expected the TIPS array to still hold keys")
        self.assertTrue(
            tips.issubset(found),
            f"i18n gate cannot see dynamically-composed keys: {sorted(tips - found)}",
        )


class StockThemeContractTest(unittest.TestCase):
    """The High Contrast theme (ADR 0060) must stay complete and legible."""

    THEME = ROOT / "themes" / "High Contrast.css"

    def test_theme_ships_and_is_registered_as_stock(self):
        self.assertTrue(self.THEME.is_file())
        from stock_themes import is_stock_theme, stock_theme_sources

        self.assertTrue(is_stock_theme(self.THEME))
        self.assertIn("High Contrast", {p.stem for p in stock_theme_sources(ROOT)})

    def test_theme_declares_every_token_the_app_declares(self):
        import re

        def tokens(path):
            text = path.read_text(encoding="utf-8")
            block = re.search(r":root\s*\{([^}]*)\}", text, re.S).group(1)
            return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

        app = tokens(ROOT / "static" / "app.css")
        self.assertTrue(app)
        self.assertEqual(
            set(),
            app - tokens(self.THEME),
            "a stock theme must redeclare every app.css :root token",
        )

    def test_theme_has_a_version_marker(self):
        # stock_themes.ensure_stock_themes skips an installed copy whose version
        # is >= the bundled one, so an unversioned theme would never refresh.
        import re

        head = self.THEME.read_text(encoding="utf-8")[:120]
        self.assertIsNotNone(
            re.search(r"/\* OpenBox Stock Theme v(\d+):", head),
            "theme needs a 'OpenBox Stock Theme vN:' marker to be refreshable",
        )

    def test_body_contrast_meets_wcag_aaa(self):
        import re

        text = self.THEME.read_text(encoding="utf-8")
        block = re.search(r":root\s*\{([^}]*)\}", text, re.S).group(1)
        values = {n: v.strip() for n, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)}

        def luminance(hex_value):
            raw = hex_value.lstrip("#")
            if len(raw) == 3:
                raw = "".join(c * 2 for c in raw)
            channels = []
            for index in (0, 2, 4):
                scaled = int(raw[index:index + 2], 16) / 255
                channels.append(
                    scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4
                )
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

        def ratio(fg, bg):
            a, b = luminance(fg), luminance(bg)
            return (max(a, b) + 0.05) / (min(a, b) + 0.05)

        bg = values["--bg"]
        # AAA is 7:1; this is a high-contrast theme, so hold it there.
        self.assertGreaterEqual(ratio(values["--text"], bg), 7.0)
        self.assertGreaterEqual(ratio(values["--muted"], bg), 7.0)



if __name__ == "__main__":
    unittest.main(verbosity=2)
