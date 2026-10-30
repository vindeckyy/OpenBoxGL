#!/usr/bin/env python3
"""Library integrity report (plan W1-11, W3-08): missing, unreadable, out-of-root, and token problems.

The report is read-only. Each category carries a count and examples, and the exit code says
whether anything is wrong (1 for warnings, 2 for errors).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pkg.state import library_v7  # noqa: E402
from state_store import default_state  # noqa: E402


class IntegrityReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.root = Path(self.tmp.name) / "games"
        (self.root / "roms").mkdir(parents=True)
        (self.root / "roms" / "present.iso").write_bytes(b"x")

    def write_library(self, games):
        state = default_state()
        state["library_root"] = str(self.root)
        state["path_mode"] = "portable"
        state["games"] = games
        (self.data / "library.json").write_text(json.dumps(state), encoding="utf-8")

    def counts(self, report):
        return {name: info["count"] for name, info in report["categories"].items()}

    def test_each_problem_is_counted_in_its_own_category(self):
        locked = self.root / "roms" / "locked.iso"
        locked.write_bytes(b"x")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        self.write_library([
            {"game_id": "ok", "name": "Ok", "path": "@library/roms/present.iso"},
            {"game_id": "missing", "name": "Missing", "path": "@library/roms/gone.iso"},
            {"game_id": "locked", "name": "Locked", "path": "@library/roms/locked.iso"},
            {"game_id": "outside", "name": "Outside", "path": "/elsewhere/outside.iso"},
            {"game_id": "escape", "name": "Escape", "path": "@library/../secret.iso"},
        ])
        report = library_v7.check_library(self.data)
        counts = self.counts(report)
        # The outside path is reported as outside the root and, since it does not exist, as missing too.
        self.assertEqual(counts.get("game_path_missing"), 2)
        self.assertEqual(counts.get("game_path_unreadable"), 1)
        self.assertEqual(counts.get("game_path_outside_root"), 1)
        self.assertEqual(counts.get("path_token_escape"), 1)
        self.assertEqual(report["exit_code"], 2)

    def test_a_missing_token_is_reported_at_its_resolved_location(self):
        self.write_library([{"game_id": "gone", "name": "Gone", "path": "@library/roms/gone.iso"}])
        report = library_v7.check_library(self.data)
        examples = report["categories"]["game_path_missing"]["examples"]
        self.assertIn(os.path.join(str(self.root), "roms", "gone.iso"), examples[0])

    def test_a_healthy_library_reports_no_categories(self):
        self.write_library([{"game_id": "ok", "name": "Ok", "path": "@library/roms/present.iso"}])
        report = library_v7.check_library(self.data)
        self.assertEqual(report["categories"], {})
        self.assertEqual(report["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
