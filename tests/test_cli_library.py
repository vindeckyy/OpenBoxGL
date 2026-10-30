"""Library CLI through web_app.py: check, migrate, roll back, and exit codes. Never starts a server."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "state" / "v6" / "one.json"


def run_cli(data_dir, *args):
    env = dict(os.environ, OPENBOX_DATA_DIR=str(data_dir))
    return subprocess.run(
        [sys.executable, "-B", str(ROOT / "web_app.py"), *args],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
    )


class LibraryCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.primary = self.data_dir / "library.json"
        self.original = FIXTURE.read_bytes()
        self.primary.write_bytes(self.original)

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_reports_a_v6_library_as_a_warning_and_changes_nothing(self):
        result = run_cli(self.data_dir, "--check-library")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "warnings")
        self.assertIn("schema_outdated", report["categories"])
        self.assertEqual(self.primary.read_bytes(), self.original)

    def test_migrate_writes_the_v6_copy_and_a_v7_primary(self):
        result = run_cli(self.data_dir, "--migrate-library")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["migrated"])
        self.assertEqual(payload["report"]["to_schema"], 7)
        self.assertEqual((self.data_dir / "library.v6.backup.json").read_bytes(), self.original)
        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["schema_version"], 7)

    def test_rollback_without_yes_refuses_and_changes_nothing(self):
        run_cli(self.data_dir, "--migrate-library")
        migrated = self.primary.read_bytes()
        result = run_cli(self.data_dir, "--rollback-library")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--yes", result.stderr)
        self.assertEqual(self.primary.read_bytes(), migrated)

    def test_rollback_with_yes_restores_the_exact_v6_bytes(self):
        run_cli(self.data_dir, "--migrate-library")
        result = run_cli(self.data_dir, "--rollback-library", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "rolled_back")
        self.assertEqual(self.primary.read_bytes(), self.original)

    def test_usage_errors_exit_four(self):
        result = run_cli(self.data_dir, "--check-library", "--migrate-library")
        self.assertEqual(result.returncode, 4)
        self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
