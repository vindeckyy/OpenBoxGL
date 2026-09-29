#!/usr/bin/env python3
"""CI gate contract tests for workflows and Dependabot."""

import contextlib
import io
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
        for job in ("shellcheck", "desktop-appstream", "flatpak-validate", "perf-20k", "ui-smoke"):
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
        self.assertIn("./scripts/ui_smoke.sh", ci, "the ui-smoke job must run the browser smoke")
        self.assertIn('"--v2", "--check"', (ROOT / "scripts" / "check_tests.py").read_text(encoding="utf-8"),
                      "the local gate must verify docs/api-v2.md is fresh")
        self.assertIn("python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5", ci)
        self.assertIn("python3 -B scripts/check_changed_coverage.py --fail-under=95", ci)

    def test_exec_bit_gate_is_wired_and_catches_a_lost_mode(self):
        """The exec bit is undetectable from a Windows checkout, so the gate
        must run in CI *and* actually fail when a mode is lost."""
        from scripts import check_exec_modes

        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("check_exec_modes.py", ci, "CI must run the exec-bit gate")
        self.assertIn("check_exec_modes.py", (ROOT / "scripts" / "check_tests.py").read_text(encoding="utf-8"),
                      "the local gate must run it too, so it is caught pre-push")

        def run_gate():
            # The gate prints one line per offender; on a real failure that
            # output is the point, but here it would bury the test output.
            with contextlib.redirect_stdout(io.StringIO()):
                return check_exec_modes.main()

        # The repo itself must be clean...
        self.assertEqual(0, run_gate(), "repo has a shebang file without the exec bit")

        # ...and it must notice a *real* 100644 from the index, not merely a
        # flipped constant. Feeding the gate a genuine non-executable mode is
        # what proves it keys on the mode rather than on anything else -- a
        # swapped comparison would pass the constant-flip test and fail this.
        original_modes = check_exec_modes.tracked_modes()
        self.assertEqual("100755", original_modes["scripts/contract_ratchet.py"],
                         "precondition: the victim starts executable")

        def with_mode(path, mode):
            patched = dict(original_modes)
            patched[path] = mode
            return patched

        original_reader = check_exec_modes.tracked_modes
        try:
            # A real 100644 on a shebang file must fail...
            check_exec_modes.tracked_modes = lambda: with_mode(
                "scripts/contract_ratchet.py", "100644")
            self.assertEqual(1, run_gate(), "gate must fail on a real 100644")

            # ...and a 120000 symlink must not be mistaken for one.
            check_exec_modes.tracked_modes = lambda: with_mode(
                "scripts/contract_ratchet.py", "120000")
            self.assertEqual(1, run_gate(), "gate must reject a non-regular mode too")
        finally:
            check_exec_modes.tracked_modes = original_reader
        self.assertEqual(0, run_gate(), "gate must pass again once restored")


    def test_exec_bit_gate_honours_a_narrowed_suffix_set(self):
        """find_offenders must respect the suffix set it is given.

        The suffix filter is the one input that can silently stop the gate
        checking whole classes of file -- the same failure mode as the bug
        this gate exists to catch. Exercised against a temp dir rather than
        the repo so the assertion cannot be satisfied by accident.
        """
        import tempfile

        from scripts import check_exec_modes

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "script.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            (root / "tool.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (root / "notes.md").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "no_shebang.py").write_text("print('x')\n", encoding="utf-8")

            modes = {
                "script.sh": "100644",
                "tool.py": "100644",
                "notes.md": "100644",
                "no_shebang.py": "100644",
            }
            # Default set: .sh and .py are checked, .md is not a script.
            default = {p for p, _ in check_exec_modes.find_offenders(modes, root)}
            self.assertEqual({"script.sh", "tool.py"}, default)

            # Narrowed to shell only: the Python file must drop out.
            shell_only = {p for p, _ in check_exec_modes.find_offenders(modes, root, (".sh",))}
            self.assertEqual({"script.sh"}, shell_only,
                             "the suffix parameter must actually narrow the scan")

            # Narrowed to a suffix nothing matches: no offenders, not all of them.
            none = check_exec_modes.find_offenders(modes, root, (".rb",))
            self.assertEqual([], none, "an unmatched suffix must select nothing, not everything")

            # A file without a shebang is never an offender, whatever the mode.
            self.assertNotIn("no_shebang.py", default)

            # A shebang file already at 100755 is never an offender.
            self.assertEqual(
                [],
                check_exec_modes.find_offenders({"script.sh": "100755"}, root),
            )

    def test_check_tests_floor_constants(self):
        from scripts import check_tests

        self.assertEqual(check_tests.COVERAGE_FLOOR, 83.0)
        self.assertEqual(check_tests.WEB_APP_FLOOR, 58.0)
        self.assertEqual(check_tests.CHANGED_LINE_FLOOR, 95.0)
        self.assertEqual(check_tests.NEW_MODULE_FLOOR, 85.0)

    def test_import_benchmark_is_warmed_and_best_of_n(self):
        """Row 47 of the reliability catalog: a wall-clock budget must not be
        measured once, cold.

        This one failed intermittently in the Windows job and passed on every
        developer machine, so the runner gained a retry to absorb it. The retry
        was the containment, not the fix: a regression back to a single cold
        sample would reappear as the same flake, so assert the measurement
        contract itself — a warm-up call, and a best-of-N assertion.
        """
        text = (ROOT / "tests" / "test_auto_import.py").read_text(encoding="utf-8")
        body = text.split("def test_large_directory_import_throughput", 1)[1]
        self.assertIn("group_multi_disc(synthetic_paths[:2000])", body,
                      "the benchmark must warm up before timing a cold call")
        self.assertIn("min(samples)", body,
                      "the benchmark must assert its best sample, not a single reading")
        self.assertIn("dedupe_ranked_imports(additions[:200])", body,
                      "dedupe imports parity_premium lazily, so it needs its own warm-up")
        self.assertIn("min(dedupe_samples)", body,
                      "dedupe must be timed the same way as grouping")
        self.assertIn("max_allowed = 0.50 if sys.gettrace() is not None else 0.25", body,
                      "the 250ms budget is the contract and must not be relaxed")


if __name__ == "__main__":
    unittest.main()
