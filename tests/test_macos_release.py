#!/usr/bin/env python3
"""macOS release gate checks (plan W5-01, ADR 0094).

The gate's pure checks are tested on the strings Apple's tools print. The repository's own
build script and entitlements file are checked as they stand, so a removed notarization step
fails the test suite as well as `make check`.
"""

from __future__ import annotations

import plistlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_macos_release as gate  # noqa: E402


class EntitlementTests(unittest.TestCase):
    def test_a_key_outside_the_adr_table_is_reported(self):
        problems = gate.entitlement_problems({"com.apple.security.cs.allow-jit": True})
        self.assertEqual(len(problems), 1)
        self.assertIn("allow-jit", problems[0])

    def test_an_empty_entitlements_dictionary_passes(self):
        self.assertEqual(gate.entitlement_problems({}), [])

    def test_the_shipped_entitlements_file_parses_and_passes(self):
        shipped = plistlib.loads(gate.ENTITLEMENTS.read_bytes())
        self.assertEqual(gate.entitlement_problems(shipped), [])


class PipelineTests(unittest.TestCase):
    def test_a_script_missing_the_stapling_step_is_reported(self):
        script = "\n".join(step for step in gate.REQUIRED_PIPELINE if step != "xcrun stapler staple")
        self.assertEqual(len(gate.pipeline_problems(script)), 1)

    def test_the_repository_build_script_keeps_every_notarization_step(self):
        self.assertEqual(gate.pipeline_problems(gate.BUILD_SCRIPT.read_text(encoding="utf-8")), [])


class SigningOutputTests(unittest.TestCase):
    def test_the_hardened_runtime_flag_is_read_from_codesign_details(self):
        self.assertTrue(gate.runtime_flag_set("CodeDirectory v=20500 size=472 flags=0x10002(adhoc,runtime) hashes=8+3"))
        self.assertFalse(gate.runtime_flag_set("CodeDirectory v=20400 size=464 flags=0x2(adhoc) hashes=8+3"))

    def test_gatekeeper_accepts_only_a_notarized_developer_id_source(self):
        self.assertTrue(gate.notarized(0, "/path: accepted\nsource=Notarized Developer ID"))
        self.assertFalse(gate.notarized(0, "/path: accepted\nsource=Developer ID"))
        self.assertFalse(gate.notarized(3, "/path: rejected\nsource=Unnotarized Developer ID"))


if __name__ == "__main__":
    unittest.main()
