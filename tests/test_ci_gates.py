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


    def test_check_tests_floor_constants(self):
        from scripts import check_tests

        self.assertEqual(check_tests.COVERAGE_FLOOR, 72.0)
        self.assertEqual(check_tests.WEB_APP_FLOOR, 54.0)
        self.assertEqual(check_tests.CHANGED_LINE_FLOOR, 80.0)
        self.assertEqual(check_tests.NEW_MODULE_FLOOR, 85.0)


if __name__ == "__main__":
    unittest.main()
