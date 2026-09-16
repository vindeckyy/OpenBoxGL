#!/usr/bin/env python3
"""CI gate contract tests for workflows and Dependabot."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHA_PIN = re.compile(r"@[0-9a-f]{40}\b")


class CiGatesTests(unittest.TestCase):
    def test_dependabot_groups_github_actions(self):
        content = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions", content)
        self.assertIn("groups:", content)

    def test_workflows_use_sha_pins(self):
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "uses:" not in line:
                    continue
                value = line.split("uses:", 1)[1].strip().split("#", 1)[0].strip()
                if "@" not in value:
                    continue
                pin = value.split("@", 1)[1]
                self.assertRegex(
                    pin,
                    r"^[0-9a-f]{40}$",
                    msg=f"{workflow.name} uses floating pin: {value}",
                )

    def test_ci_required_jobs(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for job in ("shellcheck", "desktop-appstream", "flatpak-validate", "perf-20k"):
            self.assertIsNotNone(re.search(rf"^\s+{job}:", ci, re.MULTILINE), msg=f"missing job {job}")
            block = ci.split(f"{job}:", 1)[1].split("\n  ", 1)[0]
            self.assertNotIn("continue-on-error: true", block, msg=f"{job} must not continue on error")

    def test_ci_job_commands(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("shellcheck", ci)
        self.assertIn("desktop-file-validate", ci)
        self.assertIn("appstreamcli validate", ci)
        self.assertIn("flatpak-builder --dry-run", ci)
        self.assertTrue(
            "validate_flatpak_manifest.py" in ci or "flatpak-builder --dry-run" in ci,
            "flatpak-validate must dry-run or validate the manifest",
        )
        self.assertIn("python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5", ci)
        self.assertIn("python3 -B scripts/check_changed_coverage.py --fail-under=95", ci)


    def test_check_tests_floor_constants(self):
        from scripts import check_tests

        self.assertEqual(check_tests.COVERAGE_FLOOR, 83.0)
        self.assertEqual(check_tests.WEB_APP_FLOOR, 73.0)
        self.assertEqual(check_tests.CHANGED_LINE_FLOOR, 95.0)
        self.assertEqual(check_tests.NEW_MODULE_FLOOR, 85.0)

    def test_gate_checkout_fetches_history_for_diff_base(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        gate = ci.split("\n  gate:", 1)[1].split("\n  shellcheck:", 1)[0]
        self.assertIn("fetch-depth: 0", gate, msg="gate needs full history for the diff base")
        self.assertIn("OPENBOX_DIFF_BASE", gate, msg="gate must pass the PR base SHA")

    def test_release_workflows_run_version_sync_and_gate(self):
        for name in ("release-appimage.yml", "release-flatpak.yml"):
            with self.subTest(workflow=name):
                content = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
                self.assertIn("python3 scripts/check_version_sync.py", content)
                self.assertIn("python3 scripts/check_tests.py", content)

    def test_release_signing_pin_matches_requirements_dev(self):
        requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        match = re.search(r"^cryptography==([0-9.]+)", requirements, re.MULTILINE)
        self.assertIsNotNone(match, msg="requirements-dev.txt must pin cryptography")
        pinned = match.group(1)
        appimage = (ROOT / ".github" / "workflows" / "release-appimage.yml").read_text(encoding="utf-8")
        self.assertIn(f"cryptography=={pinned}", appimage, msg="release signer must use the dev pin")

    def test_flatpak_release_attests_bundle_before_publish(self):
        flatpak = (ROOT / ".github" / "workflows" / "release-flatpak.yml").read_text(encoding="utf-8")
        self.assertIn("\n  attest:", flatpak, msg="flatpak release needs a provenance attestation job")
        attest_block = flatpak.split("\n  attest:", 1)[1].split("\n  publish:", 1)[0]
        self.assertIn("attest-build-provenance", attest_block)
        self.assertIn("id-token: write", attest_block)
        self.assertIn("needs: [build, attest]", flatpak, msg="publish must wait for attestation")

    def test_readme_ci_badge_is_live(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("actions/workflows/ci.yml/badge.svg", readme)
        self.assertNotIn("badge/CI-passing", readme)

    def test_makefile_check_ci_and_isolated_test_one(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("check-ci:", makefile)
        self.assertIn("shellcheck", makefile)
        self.assertIn("perf_bench.py --sizes 10000,20000 --runs 5", makefile)
        test_one = makefile.split("test-one:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("OPENBOX_DATA_DIR", test_one, msg="test-one must isolate state")
        self.assertIn("requirements.stamp", makefile, msg="dev-venv must be conditional")

    def test_gate_wires_docs_links_api_docs_and_touched_module_floor(self):
        gate = (ROOT / "scripts" / "check_tests.py").read_text(encoding="utf-8")
        self.assertIn("check_docs_links.py", gate)
        self.assertIn("gen_api_docs.py", gate)
        self.assertIn("--check", gate)
        self.assertIn("check_changed_coverage.py", gate)

    def test_gate_reports_flakes_timeouts_and_skips(self):
        from scripts import check_tests

        notes = check_tests._skip_notes(
            "test_skips_zero ok\nskipped 'gamescope not installed'\ngamescope not installed; skipping Deck emulation\n"
        )
        self.assertEqual(len(notes), 2)
        self.assertEqual(check_tests._skip_notes("all good\n"), [])

    def test_run_all_tests_has_timeout_and_skip_accounting(self):
        script = (ROOT / "run_all_tests.sh").read_text(encoding="utf-8")
        self.assertIn("timeout", script)
        self.assertIn("TIMEOUT", script)
        self.assertIn("OPENBOX_STRICT_SKIPS", script)
        self.assertIn("skip note", script)


if __name__ == "__main__":
    unittest.main()
