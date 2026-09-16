#!/usr/bin/env python3
"""Tests for scripts/bump_version.py (dry-run default, --apply writes)."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import bump_version  # noqa: E402


def _capture(func, *args, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(*args, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


class PlannerTests(unittest.TestCase):
    def test_updates_py_plan(self):
        text, count = bump_version.plan_updates_py('VERSION = "1.12.1"\n', "1.13.0")
        self.assertEqual(count, 1)
        self.assertIn('VERSION = "1.13.0"', text)

    def test_readme_plan_touches_badge_link_and_installer(self):
        readme = (
            "img.shields.io/badge/Release-v1.12.1\n"
            "Latest stable: v1.12.1\n"
            "VERSION=1.12.1\n"
        )
        text, count = bump_version.plan_readme(readme, "1.13.0")
        self.assertEqual(count, 3)
        self.assertIn("Release-v1.13.0", text)
        self.assertIn("Latest stable: v1.13.0", text)
        self.assertIn("VERSION=1.13.0", text)

    def test_readme_plan_leaves_feature_headings_alone(self):
        readme = "### Living Library (1.12.0)\nLatest stable: v1.12.1\n"
        text, count = bump_version.plan_readme(readme, "1.13.0")
        self.assertEqual(count, 1)
        self.assertIn("### Living Library (1.12.0)", text)

    def test_metainfo_plan_updates_only_the_latest_release(self):
        meta = '<release version="1.12.1" date="a">\n<release version="1.12.0" date="b">\n'
        text, count = bump_version.plan_metainfo(meta, "1.13.0")
        self.assertEqual(count, 1)
        self.assertIn('<release version="1.13.0" date="a">', text)
        self.assertIn('<release version="1.12.0" date="b">', text)

    def test_changelog_ready_accepts_dated_section(self):
        ready, reason = bump_version.changelog_ready("## [1.13.0] - 2026-09-16\n- x\n", "1.13.0")
        self.assertTrue(ready)
        self.assertIn("dated", reason)

    def test_changelog_ready_accepts_nonempty_unreleased(self):
        ready, _reason = bump_version.changelog_ready("## [Unreleased]\n\n### Added\n- x\n", "1.13.0")
        self.assertTrue(ready)

    def test_changelog_ready_rejects_empty_unreleased(self):
        ready, reason = bump_version.changelog_ready("## [Unreleased]\n\n### Added\n", "1.13.0")
        self.assertFalse(ready)
        self.assertIn("no [1.13.0] section", reason)


class MainTests(unittest.TestCase):
    def _tree(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "docs").mkdir()
        (root / "updates.py").write_text('VERSION = "1.12.1"\n', encoding="utf-8")
        (root / "README.md").write_text(
            "img.shields.io/badge/Release-v1.12.1\nLatest stable: v1.12.1\nVERSION=1.12.1\n",
            encoding="utf-8",
        )
        (root / "openbox.metainfo.xml").write_text('<release version="1.12.1" date="a">\n', encoding="utf-8")
        (root / "docs" / "CHANGELOG.md").write_text(
            "## [Unreleased]\n\n### Added\n- something\n", encoding="utf-8"
        )
        return root

    def test_invalid_version_is_rejected(self):
        code, _out, err = _capture(bump_version.main, ["1.13"])
        self.assertEqual(code, 2)
        self.assertIn("invalid version", err)

    def test_dry_run_writes_nothing(self):
        root = self._tree()
        with mock.patch.object(bump_version, "ROOT", root):
            code, out, _err = _capture(bump_version.main, ["1.13.0"])
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual((root / "updates.py").read_text(encoding="utf-8"), 'VERSION = "1.12.1"\n')
        self.assertIn("v1.12.1", (root / "README.md").read_text(encoding="utf-8"))

    def test_apply_updates_all_mechanical_spots(self):
        root = self._tree()
        with mock.patch.object(bump_version, "ROOT", root):
            code, _out, _err = _capture(bump_version.main, ["1.13.0", "--apply"])
        self.assertEqual(code, 0)
        self.assertIn('VERSION = "1.13.0"', (root / "updates.py").read_text(encoding="utf-8"))
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("Release-v1.13.0", readme)
        self.assertIn("Latest stable: v1.13.0", readme)
        self.assertIn("VERSION=1.13.0", readme)
        self.assertIn('<release version="1.13.0"', (root / "openbox.metainfo.xml").read_text(encoding="utf-8"))

    def test_apply_refuses_without_changelog_entries(self):
        root = self._tree()
        (root / "docs" / "CHANGELOG.md").write_text("## [Unreleased]\n\n### Added\n", encoding="utf-8")
        with mock.patch.object(bump_version, "ROOT", root):
            code, _out, err = _capture(bump_version.main, ["1.13.0", "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("changelog", err)
        self.assertEqual((root / "updates.py").read_text(encoding="utf-8"), 'VERSION = "1.12.1"\n')


if __name__ == "__main__":
    unittest.main()
