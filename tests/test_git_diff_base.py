#!/usr/bin/env python3
"""Tests for scripts/git_diff_base.py and the changed-line gate failure modes.

Regression coverage for the 1.12.x gate hole: on pull requests the diff base
resolved to HEAD, so changed-line and new-module coverage silently passed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_changed_coverage  # noqa: E402
from scripts.git_diff_base import DiffBaseUnresolved, resolve_diff_base  # noqa: E402

GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git is required for diff-base tests")
class GitDiffBaseTests(unittest.TestCase):
    def setUp(self):
        env = {"OPENBOX_DIFF_BASE": "", "GITHUB_BASE_SHA": "", "GITHUB_EVENT_PATH": ""}
        self._env_patch = mock.patch.dict(os.environ, env)
        self._env_patch.start()
        for key in env:
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory(prefix="openbox-diffbase.")
        self.extra_tmp = []
        self.repo = Path(self.tmp.name)
        self.commits = []
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "OpenBox Test")
        self._commit("a.py", "print('a')\n")
        self._commit("a.py", "print('a')\nprint('b')\n")

    def tearDown(self):
        self._env_patch.stop()
        for tmp in self.extra_tmp:
            tmp.cleanup()
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            [GIT, *args], cwd=self.repo, capture_output=True, text=True, check=False
        )

    def _commit(self, name, content):
        (self.repo / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-qm", f"edit {name}")
        self.commits.append(self._git("rev-parse", "HEAD").stdout.strip())

    def _run(self, command):
        return subprocess.run(command, cwd=self.repo, capture_output=True, text=True, check=False)

    def _add_bare_origin(self):
        tmp = tempfile.TemporaryDirectory(prefix="openbox-diffbase-origin.")
        self.extra_tmp.append(tmp)
        bare = Path(tmp.name) / "origin.git"
        subprocess.run([GIT, "init", "-q", "--bare", str(bare)], check=True)
        self._git("remote", "add", "origin", str(bare))
        self._git("push", "-q", "origin", "master")
        return bare

    def test_explicit_ref_wins(self):
        self.assertEqual(resolve_diff_base("bogus-ref", run=self._run), "bogus-ref")

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"OPENBOX_DIFF_BASE": self.commits[0]}):
            self.assertEqual(resolve_diff_base(run=self._run), self.commits[0])

    def test_no_remote_no_upstream_returns_none(self):
        self.assertIsNone(resolve_diff_base(run=self._run))

    def test_origin_master_fallback_uses_merge_base(self):
        self._add_bare_origin()
        self._git("checkout", "-q", "-b", "feature")
        self._commit("feature.py", "print('feature')\n")
        self._git("remote", "set-head", "origin", "--delete")
        self.assertEqual(resolve_diff_base(run=self._run), self.commits[1])

    def test_upstream_merge_base(self):
        self._add_bare_origin()
        self._git("checkout", "-q", "-b", "feature")
        self._git("branch", "--set-upstream-to=origin/master", "feature")
        self._commit("feature.py", "print('feature')\n")
        self.assertEqual(resolve_diff_base(run=self._run), self.commits[1])

    def test_github_event_payload_without_remote(self):
        event = Path(self.tmp.name + ".event.json")
        self.addCleanup(event.unlink, missing_ok=True)
        event.write_text(
            '{"pull_request": {"base": {"sha": "%s"}}}' % self.commits[0],
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}):
            self.assertEqual(resolve_diff_base(run=self._run), self.commits[0])

    def test_changed_python_files_raises_on_bad_base(self):
        with mock.patch.object(check_changed_coverage, "_run", self._run):
            with self.assertRaises(RuntimeError):
                check_changed_coverage._changed_python_files("does-not-exist")

    def test_measure_changed_lines_requires_a_base(self):
        with mock.patch.object(check_changed_coverage, "_run", self._run):
            with self.assertRaises(DiffBaseUnresolved):
                check_changed_coverage.measure_changed_lines()


if __name__ == "__main__":
    unittest.main()
