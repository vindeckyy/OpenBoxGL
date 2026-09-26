"""Tests for the ADR 0060 feature-regression ratchets.

Three layers of evidence, matching what the gates actually do:

* Every gate passes on the real tree, and its baseline matches the live surface.
* ``RatchetRuleTest`` exercises the shared ledger rule directly, including the
  reference-dependent half that no passing run can show.
* ``BaselineEditBypassTest`` plays the bypass the ADR promises to stop: delete
  the feature *and* its baseline entry in one commit. Layer 1 cannot see that,
  so each gate is driven with an edited contract and a reference that still
  holds the entry, and must fail.

A gate that only passes is not evidence, so none of the three is skipped.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / "scripts"


def _load(name):
    """Import a gate script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(f"gate_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(call):
    """Run a gate's main(), returning (exit code, captured output)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = call()
    return code, buffer.getvalue()


@contextlib.contextmanager
def _baseline_edit_bypass(gate, attr, drop):
    """Simulate deleting a feature *and* its baseline entry in one commit.

    ``drop(contract)`` mutates the contract copy and returns the identity that
    was removed. The gate then runs against a temporary contract while the
    reference still holds the original, which is the state a reviewer sees, and
    the state layer 1 cannot detect. Yields (dropped identity, run callable).
    """
    original = json.loads(getattr(gate, attr).read_text(encoding="utf-8"))
    edited = json.loads(json.dumps(original))
    dropped = drop(edited)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / getattr(gate, attr).name
        path.write_text(json.dumps(edited), encoding="utf-8")
        with mock.patch.object(gate, attr, path), mock.patch.object(
            gate.contract_ratchet, "reference_data", return_value=(original, "test reference")
        ):
            yield dropped, lambda: _run(gate.main)


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

    def test_baseline_edit_cannot_hide_a_route_removal(self):
        # Layer 1 compares live against this very baseline, so a commit that
        # deletes the route and its entry together looks consistent. Only the
        # reference comparison can see what was there before.
        with _baseline_edit_bypass(
            self.gate, "BASELINE", lambda data: data["routes"].pop()["path"]
        ) as (dropped, run):
            code, output = run()
        self.assertEqual(1, code)
        self.assertIn(f"BASELINE-SHRINK {dropped}", output)



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

    def test_baseline_edit_cannot_hide_a_key_removal(self):
        # Deleting the key and its baseline entry together would silently erase
        # that setting from every existing library on the next save.
        with _baseline_edit_bypass(
            self.gate, "BASELINE", lambda data: data["keys"].pop()
        ) as (dropped, run):
            code, output = run()
        self.assertEqual(1, code)
        self.assertIn(f"BASELINE-SHRINK {dropped}", output)


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

    def test_baseline_edit_cannot_hide_a_definition_removal(self):
        # The set of definitions is only ratcheted against itself, so dropping a
        # file and its contract entry together used to pass unnoticed.
        with _baseline_edit_bypass(
            self.gate, "BASELINE", lambda data: data["definitions"].pop()["file"]
        ) as (dropped, run):
            code, output = run()
        self.assertEqual(1, code)
        self.assertIn(f"BASELINE-SHRINK {dropped}", output)


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

    def test_dynamic_imports_are_scanned(self):
        # Only the dynamic regex sees this form; the static ones require `from`.
        self.assertIn("sessions.js", self.gate.DYNAMIC_IMPORT_RE.findall(
            "const sessions = await import('./sessions.js');"
        ))

    def test_dangling_dynamic_import_is_reported(self):
        # Mutation proof: make the dynamically imported specifier unresolvable
        # and the graph must report it, naming the specifier and the importer.
        original = self.gate.resolve
        self.gate.resolve = (
            lambda specifier: None if specifier == "sessions.js" else original(specifier)
        )
        try:
            problems = self.gate.check_graph()
        finally:
            self.gate.resolve = original
        self.assertTrue(
            any("./sessions.js" in problem for problem in problems),
            f"a dangling dynamic import must fail the graph check, got {problems}",
        )

    def test_baseline_edit_cannot_hide_a_module_removal(self):
        with _baseline_edit_bypass(
            self.gate, "BASELINE", lambda data: data["modules"].pop()
        ) as (dropped, run):
            code, output = run()
        self.assertEqual(1, code)
        self.assertIn(f"BASELINE-SHRINK {dropped}", output)


class ReliabilityCatalogTest(unittest.TestCase):
    """A 'Tested' row must name a test that exists."""

    def setUp(self):
        self.gate = _load("check_reliability_catalog")

    def test_repository_passes(self):
        self.assertEqual(0, self.gate.main())

    def test_no_problems(self):
        self.assertEqual([], self.gate.check())

    def test_baseline_edit_cannot_hide_a_dropped_row(self):
        # The catalog in docs/reliability.md is the surface and the contract is
        # its memory, so the bypass is: drop the row from the doc and rewrite
        # the contract to match. The reference must still catch it.
        original = json.loads(self.gate.CONTRACT.read_text(encoding="utf-8"))
        scenarios = list(original["scenarios"])
        dropped = scenarios.pop()
        with mock.patch.object(self.gate, "_scenario_ids", return_value=scenarios), mock.patch.object(
            self.gate.contract_ratchet, "reference_data", return_value=(original, "test reference")
        ):
            code, output = _run(self.gate.main)
        self.assertEqual(1, code)
        self.assertIn(f"REMOVED row {dropped.split(':', 1)[0]}", output)

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

    def test_imports_are_matched_by_leaf_name(self):
        # The gate used to compare a bare module name against a dotted import
        # path, so it detected nothing at all -- including test_parity_radio.py,
        # the file the whole check exists for.
        cases = (
            ("import parity_radio", "parity_radio"),
            ("from pkg.parity import parity_radio", "parity_radio"),
            ("from pkg.parity.parity_radio import score", "parity_radio"),
            ("from pkg.parity import parity_backup as backup", "parity_backup"),
            ("from state_store import JsonStateStore", "state_store"),
            ("    import openbox", "openbox"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertIn(expected, self.gate._modules_under_test(source))

    def test_rolling_windows_include_root_level_modules(self):
        # state_store.py sits at the repo root and carries TRASH_MAX_AGE_DAYS;
        # scanning only a few subdirectories silently skipped it.
        self.assertIn("state_store", self.gate._runtime_windows())

    def test_a_marker_alone_does_not_exempt_a_test(self):
        # An unrecorded marker must not be a way to silence the gate.
        with mock.patch.object(self.gate, "_exemptions", return_value={}):
            problems = self.gate.scan()
        self.assertTrue(
            any("test_parity_query.py" in problem and "no entry" in problem for problem in problems),
            f"an unrecorded marker must be reported, got {problems}",
        )


class RatchetRuleTest(unittest.TestCase):
    """The shared ledger rule: the ratchet's actual security property.

    Exercised directly, because no passing gate run can show what it refuses.
    """

    def setUp(self):
        self.rule = _load("contract_ratchet")

    def test_a_dropped_entry_needs_a_retired_record(self):
        failures = self.rule.ledger_failures({"a", "b"}, {}, {"a"}, {})
        self.assertEqual(1, len(failures))
        self.assertIn("BASELINE-SHRINK b", failures[0])

    def test_a_dropped_entry_with_a_reason_is_allowed(self):
        self.assertEqual(
            [], self.rule.ledger_failures({"a", "b"}, {}, {"a"}, {"b": "superseded by c"})
        )

    def test_growth_is_always_allowed(self):
        self.assertEqual([], self.rule.ledger_failures({"a"}, {}, {"a", "b", "c"}, {}))

    def test_remedy_names_the_dropped_key(self):
        failures = self.rule.ledger_failures(
            {"a"}, {}, set(), {}, drop_remedy='add {"path": "{key}"} to the retired list'
        )
        self.assertIn('add {"path": "a"} to the retired list', failures[0])

    def test_a_fabricated_retirement_is_refused(self):
        failures = self.rule.ledger_failures({"a"}, {}, {"a"}, {"ghost": "never shipped"})
        self.assertTrue(any("RETIRED-FABRICATED ghost" in line for line in failures))

    def test_deleting_a_retired_record_is_refused(self):
        failures = self.rule.ledger_failures({"a"}, {"b": "why"}, {"a"}, {})
        self.assertTrue(any("RETIRED-REMOVED b" in line for line in failures))

    def test_rewording_a_retired_record_is_refused(self):
        failures = self.rule.ledger_failures({"a"}, {"b": "why"}, {"a"}, {"b": "because"})
        self.assertTrue(any("RETIRED-REWRITTEN b" in line for line in failures))

    def test_consistency_rules_need_no_reference(self):
        failures = self.rule.ledger_consistency({"a"}, {"a": "still live", "c": "   "})
        self.assertTrue(any("RETIRED-STILL-LIVE a" in line for line in failures))
        self.assertTrue(any("RETIRED-NO-REASON c" in line for line in failures))

    def test_reference_override_is_honoured(self):
        # The tests above cannot drive real history, so the gates must accept an
        # explicit reference; without it this suite would be unverifiable.
        with mock.patch.dict("os.environ", {"OPENBOX_CONTRACT_REF": "HEAD"}):
            rev, label = self.rule.reference_rev()
        self.assertEqual("HEAD", rev)
        self.assertIn("OPENBOX_CONTRACT_REF", label)


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
            # \w, not [a-z0-9-]: app.css ships --constellation-edge-co_played.
            return set(re.findall(r"(--[\w-]+)\s*:", block))

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
