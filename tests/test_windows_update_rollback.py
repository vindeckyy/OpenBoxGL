#!/usr/bin/env python3
"""S15 -- a failed Windows update left no installation at all.

`_applier_script` generates the PowerShell that swaps the staged tree in after
the app exits. It moved the old tree to ``<target>.previous`` and *then* moved
the new tree in, with no restore. Moving the second tree is exactly the step
that can fail for reasons outside this process -- Defender holding a file, the
disk filling, a locked DLL -- and by then the first step had already succeeded.

The user was told the update installed: `_install_update_windows` had already
returned ``{"installed": ...}``. In fact the install directory no longer
existed, and the only copy of their installation was a directory named
``.previous`` that nothing knew to look for.

The second half: an attempt that never reached its applier at all (the app was
killed, the machine rebooted) leaked its ``.{name}.next-*`` scratch tree and the
extracted payload next to the install, a few hundred MB per attempt.

The invariant: **a failed update leaves the installation exactly as it was.**
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import updates

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
IS_WINDOWS = os.name == "nt"


def _run_script(script: str, workdir: Path) -> subprocess.CompletedProcess:
    path = workdir / "applier.ps1"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(path)],
        capture_output=True, text=True, timeout=120, check=False,
    )


@unittest.skipUnless(IS_WINDOWS and POWERSHELL, "Windows applier script is platform-specific")
class ApplierRollbackTests(unittest.TestCase):
    """The generated PowerShell is executed, not just inspected."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # target.parent must exist for the glob sweep and the scratch dir.
        self.target = self.root / "OpenBox"
        self.target.mkdir()
        (self.target / "web_app.py").write_text("OLD VERSION\n", encoding="utf-8")
        (self.target / "openbox.exe").write_bytes(b"OLD EXE")
        # A pid that is certainly not running, so the wait loop exits at once.
        self.dead_pid = 0x7FFFFFFF

    def _staged(self, content="NEW VERSION\n", create=True):
        staged = self.root / "staged"
        if create:
            staged.mkdir(exist_ok=True)
            (staged / "web_app.py").write_text(content, encoding="utf-8")
        return staged

    def _script(self, staged, scratch):
        return updates._applier_script(self.dead_pid, self.target, staged, scratch)

    def _previous(self):
        return Path(f"{self.target}.previous")

    def test_a_successful_swap_installs_the_new_tree(self):
        staged = self._staged()
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = _run_script(self._script(staged, scratch), self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.target / "web_app.py").read_text(encoding="utf-8"), "NEW VERSION\n",
            "the new tree was not installed",
        )
        self.assertTrue(
            (self._previous() / "web_app.py").is_file(),
            "the previous tree was not kept as a rollback copy",
        )

    def test_a_failed_swap_restores_the_old_tree(self):
        """The reported bug: no install directory, only a `.previous` one."""
        staged = self._staged(create=False)   # the second Move-Item will fail
        scratch = self.root / "scratch"
        scratch.mkdir()

        result = _run_script(self._script(staged, scratch), self.root)

        self.assertNotEqual(result.returncode, 0, "the applier reported success on a failed swap")
        self.assertTrue(
            self.target.is_dir(),
            f"the install directory is gone after a failed update: {sorted(p.name for p in self.root.iterdir())}",
        )
        self.assertEqual(
            (self.target / "web_app.py").read_text(encoding="utf-8"), "OLD VERSION\n",
            "the old tree was not restored",
        )
        self.assertTrue((self.target / "openbox.exe").is_file(), "the old tree is incomplete")

    def test_a_failed_swap_leaves_no_stranded_previous_directory(self):
        staged = self._staged(create=False)
        scratch = self.root / "scratch"
        scratch.mkdir()
        _run_script(self._script(staged, scratch), self.root)
        self.assertFalse(
            self._previous().exists(),
            "the rolled-back install still has a stranded .previous copy",
        )

    def test_a_failed_swap_cleans_up_the_scratch_tree(self):
        staged = self._staged(create=False)
        scratch = self.root / "scratch"
        scratch.mkdir()
        (scratch / "leftover.bin").write_bytes(b"x" * 1024)
        _run_script(self._script(staged, scratch), self.root)
        self.assertFalse(scratch.exists(), "the scratch tree was left behind after a failed swap")

    def test_a_stale_previous_copy_is_replaced_not_merged(self):
        """A second update must not fail because `.previous` already exists."""
        stale = self._previous()
        stale.mkdir()
        (stale / "web_app.py").write_text("ANCIENT\n", encoding="utf-8")

        staged = self._staged()
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = _run_script(self._script(staged, scratch), self.root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self._previous() / "web_app.py").read_text(encoding="utf-8"), "OLD VERSION\n",
        )
        self.assertNotIn("ANCIENT\n", (self._previous() / "web_app.py").read_text(encoding="utf-8"))


class ApplierScriptShapeTests(unittest.TestCase):
    """Structural checks, so the guarantees hold even off Windows."""

    def setUp(self):
        if not (IS_WINDOWS and POWERSHELL):
            self.skipTest("the applier script is only defined on Windows")

    def _script(self):
        return updates._applier_script(
            1, Path("C:/OpenBox"), Path("C:/OpenBox/.next/payload"), Path("C:/OpenBox/.next")
        )

    def test_the_swap_is_wrapped_so_a_failure_can_be_caught(self):
        script = self._script()
        self.assertIn("try {", script)
        self.assertIn("catch {", script)

    def test_the_restore_puts_the_previous_tree_back(self):
        script = self._script()
        self.assertIn(
            "Move-Item -LiteralPath $previous -Destination $target", script,
            "the catch block does not restore the old tree",
        )

    def test_the_restore_is_guarded_so_it_cannot_clobber_a_good_install(self):
        script = self._script()
        self.assertIn(
            "if ((-not (Test-Path -LiteralPath $target)) -and (Test-Path -LiteralPath $previous))",
            script,
            "the restore must only run when the target is actually missing",
        )

    def test_a_failure_exits_nonzero(self):
        """The applier must not report success after rolling back."""
        catch_body = self._script().split("catch {", 1)[1]
        self.assertIn("exit 1", catch_body)

    def test_the_wait_loop_still_runs_before_the_swap(self):
        script = self._script()
        self.assertLess(
            script.index("Get-Process -Id $targetPid"),
            script.index("try {"),
            "the swap must wait for the running app to exit first",
        )


class StaleScratchSweepTests(unittest.TestCase):
    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("the Windows applier is platform-specific")

    def test_install_sweeps_scratch_trees_from_attempts_that_never_ran(self):
        """A leaked `.OpenBox.next-*` is a few hundred MB next to the install."""
        import inspect

        source = inspect.getsource(updates._install_update_windows)
        self.assertIn(
            'glob(f".{target.name}.next-*")', source,
            "an attempt that never reached its applier leaks its scratch tree forever",
        )
        self.assertIn("shutil.rmtree", source)


if __name__ == "__main__":
    unittest.main()
