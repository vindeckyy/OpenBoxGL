"""Tests for the ADR 0049 feature-regression ratchets.

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
