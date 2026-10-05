#!/usr/bin/env python3
"""S10 -- `install()` claimed all-or-nothing and was not.

Validation was all-or-nothing. The write phase was not. Definitions were written
with bare ``destination.write_text(...)``, retractions were ``unlink``ed, and the
ownership ledger was committed **last**. Two failure modes followed:

* **A.** An AV scanner holding the ledger raises ``PermissionError`` at
  ``os.replace`` -- *after* every definition was already overwritten. The caller
  maps ``OSError`` to a 400, so the user is told the update failed while their
  definition set has in fact been replaced.
* **B.** A crash anywhere between the file writes and the ledger write leaves the
  ledger naming the *old* set. The next ``install()`` sees
  ``destination.exists()`` and ``name not in channel_owned`` and reclassifies
  every new file as a **user edit** into ``kept`` -- permanently. And
  ``rollback()`` only deletes what the stale ledger lists, so the orphans survive
  a rollback and keep shadowing the bundled set forever.

The invariant: **the directory and the ledger must always agree.** Either both
move to the new state, or neither does.
"""

import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity import parity_emulator_defs_update as defs_update

#: Every key `validate_definition` requires, shaped like a real bundled
#: definition. The install channel enforces the same list the emulator-defs gate
#: does, so a fixture missing any of them is rejected before it reaches the
#: write phase -- which would make these tests pass for the wrong reason.
VALID = """\
schema_version: 1
adapter_id: testemu
emulator_id: org.test.TestEmu
label: Test Emulator
platform: Nintendo Entertainment System
extensions:
  - nes
native_exe: testemu
native_exe_windows: TestEmu.exe
flatpak_app_id: org.test.TestEmu
startup_args:
  - "{path}"
recommended: true
priority: 10
executable_patterns:
  - testemu
state:
  kind: none
"""


def _pack(definitions):
    """A tar.gz shaped like the real community pack, returned as bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in definitions.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"definitions/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class DefsInstallTransactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.target = defs_update.local_defs_dir(self.data_dir)
        self.target.mkdir(parents=True, exist_ok=True)

    def _install(self, definitions, version="2.0.0"):
        """Run install() with the network and the verifier stubbed out."""
        archive = _pack(definitions)
        with patch.object(defs_update, "download_and_verify", return_value=archive), \
             patch.object(defs_update, "_index_version", return_value=version):
            return defs_update.install(data_dir=self.data_dir)

    def _ledger(self):
        return defs_update._read_state(self.target)

    def _contents(self):
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(self.target.glob("*.yaml"))}

    def _stray_files(self):
        """Anything in the directory that is not a definition or the ledger."""
        return sorted(
            p.name for p in self.target.iterdir()
            if not (p.suffix == ".yaml" or p.name == defs_update.STATE_FILE)
        )


class HappyPathTests(DefsInstallTransactionTests):
    def test_a_fresh_install_writes_every_definition(self):
        result = self._install({"a.yaml": VALID, "b.yaml": VALID})
        self.assertEqual(sorted(result["installed"]), ["a.yaml", "b.yaml"])
        self.assertEqual(self._ledger()["installed"], ["a.yaml", "b.yaml"])
        self.assertEqual(sorted(self._contents()), ["a.yaml", "b.yaml"])

    def test_a_refreshed_definition_is_replaced_and_recorded(self):
        self._install({"a.yaml": VALID})
        result = self._install({"a.yaml": VALID + "# v2\n"}, version="3.0.0")
        self.assertEqual(result["updated"], ["a.yaml"])
        self.assertIn("# v2", (self.target / "a.yaml").read_text(encoding="utf-8"))
        self.assertEqual(self._ledger()["version"], "3.0.0")

    def test_a_user_edit_is_never_clobbered(self):
        (self.target / "mine.yaml").write_text("hand written\n", encoding="utf-8")
        result = self._install({"mine.yaml": VALID})
        self.assertEqual(result["kept_local"], ["mine.yaml"])
        self.assertEqual(
            (self.target / "mine.yaml").read_text(encoding="utf-8"), "hand written\n"
        )
        self.assertNotIn("mine.yaml", self._ledger()["installed"])

    def test_a_retracted_definition_is_removed_and_forgotten(self):
        self._install({"a.yaml": VALID, "b.yaml": VALID})
        result = self._install({"a.yaml": VALID}, version="4.0.0")
        self.assertEqual(result["removed"], ["b.yaml"])
        self.assertFalse((self.target / "b.yaml").exists())
        self.assertEqual(self._ledger()["installed"], ["a.yaml"])

    def test_no_backup_or_temp_files_are_left_behind(self):
        self._install({"a.yaml": VALID, "b.yaml": VALID})
        self._install({"a.yaml": VALID, "c.yaml": VALID}, version="5.0.0")
        self.assertEqual(self._stray_files(), [], f"stray files: {self._stray_files()}")

    def test_rollback_after_install_removes_exactly_what_was_installed(self):
        (self.target / "mine.yaml").write_text("hand written\n", encoding="utf-8")
        self._install({"a.yaml": VALID, "mine.yaml": VALID})
        defs_update.rollback(data_dir=self.data_dir)
        self.assertFalse((self.target / "a.yaml").exists())
        self.assertTrue(
            (self.target / "mine.yaml").exists(),
            "rollback deleted a file the user owns",
        )


class LedgerWriteFailsTests(DefsInstallTransactionTests):
    """Trigger A: the ledger rename is blocked after every file was written."""

    def _install_with_ledger_failure(self, definitions, version="2.0.0"):
        archive = _pack(definitions)
        real_replace = os.replace
        state_file = defs_update.STATE_FILE

        def blocking_replace(source, destination):
            # Fail only the *ledger* commit, which is the last thing install()
            # does -- exactly where an AV scanner holding the file would fail.
            if Path(destination).name == state_file:
                raise PermissionError(13, "file is in use", str(destination))
            return real_replace(source, destination)

        with patch.object(defs_update, "download_and_verify", return_value=archive), \
             patch.object(defs_update, "_index_version", return_value=version), \
             patch.object(defs_update.os, "replace", side_effect=blocking_replace):
            with self.assertRaises(PermissionError):
                defs_update.install(data_dir=self.data_dir)

    def test_a_failed_ledger_write_leaves_the_previous_definitions_intact(self):
        self._install({"a.yaml": VALID}, version="1.0.0")
        before = self._contents()
        ledger_before = self._ledger()

        self._install_with_ledger_failure({"a.yaml": VALID + "# NEW\n", "b.yaml": VALID})

        self.assertEqual(
            self._contents(), before,
            "the definitions were replaced even though the install reported failure",
        )
        self.assertEqual(self._ledger(), ledger_before)

    def test_a_failed_first_install_leaves_nothing_behind(self):
        self._install_with_ledger_failure({"a.yaml": VALID, "b.yaml": VALID})
        self.assertEqual(self._contents(), {}, "a failed first install wrote definitions")
        self.assertEqual(self._ledger(), {})

    def test_the_next_install_after_a_ledger_failure_still_works(self):
        """The stale-ledger trap: a later run must not treat new files as edits."""
        self._install({"a.yaml": VALID}, version="1.0.0")
        self._install_with_ledger_failure({"a.yaml": VALID, "b.yaml": VALID})
        result = self._install({"a.yaml": VALID, "b.yaml": VALID}, version="2.0.0")
        self.assertEqual(
            sorted(result["installed"] + result["updated"]), ["a.yaml", "b.yaml"]
        )
        self.assertEqual(
            sorted(result["kept_local"]), [],
            "files the channel installed were reclassified as user edits",
        )


class RenameFailsMidwayTests(DefsInstallTransactionTests):
    """Trigger B: a failure partway through the renames."""

    def _install_failing_on_nth_replace(self, definitions, fail_after, version="2.0.0"):
        """Raise OSError on exactly the `fail_after`-th os.replace, then behave.

        Failing *once* is what models the crash this test is about: the install
        aborts at that step and the rollback path still has to run. A mock that
        kept raising would also break the undo's own renames, so the test would
        pass for the wrong reason by never exercising a completed rollback.

        Returns True if the injected failure actually fired. A point past the
        last rename is not an error -- it is a position the install never reaches.
        """
        archive = _pack(definitions)
        real_replace = os.replace
        calls = {"n": 0}
        fired = {"value": False}

        def counting_replace(source, destination):
            calls["n"] += 1
            if calls["n"] == fail_after:
                fired["value"] = True
                raise OSError(28, "No space left on device", str(destination))
            return real_replace(source, destination)

        with patch.object(defs_update, "download_and_verify", return_value=archive), \
             patch.object(defs_update, "_index_version", return_value=version), \
             patch.object(defs_update.os, "replace", side_effect=counting_replace):
            try:
                defs_update.install(data_dir=self.data_dir)
            except OSError:
                pass
        return fired["value"]

    def test_every_failure_point_restores_the_previous_state(self):
        """Crash at every step -> the directory and ledger are unchanged.

        Points at or past the last rename are checked too, but for the opposite
        outcome: the install *succeeds*, so the directory must hold the new pack.
        Asserting only the rollback would let a "fix" that never rolls back pass
        every point simply by not being reached.
        """
        fired_any = 0
        for fail_after in range(1, 8):
            with self.subTest(fail_after=fail_after):
                # A fresh directory per point: a failure that failed to roll back
                # would otherwise poison every later point and hide itself.
                self.setUp()
                self._install({"a.yaml": VALID, "b.yaml": VALID}, version="1.0.0")
                before = self._contents()
                ledger_before = self._ledger()

                fired = self._install_failing_on_nth_replace(
                    {"a.yaml": VALID + "# NEW\n", "c.yaml": VALID}, fail_after
                )
                self.assertEqual(
                    self._stray_files(), [],
                    f"stray files after replace #{fail_after}",
                )
                if fired:
                    fired_any += 1
                    self.assertEqual(
                        self._contents(), before,
                        f"definitions differ after a failure at replace #{fail_after}",
                    )
                    self.assertEqual(
                        self._ledger(), ledger_before,
                        f"ledger differs after a failure at replace #{fail_after}",
                    )
                else:
                    # The failure point was never reached: the install committed,
                    # so the new pack must be on disk and recorded.
                    self.assertEqual(
                        sorted(self._contents()), ["a.yaml", "c.yaml"],
                        f"replace #{fail_after} was never reached but the pack did not land",
                    )
                    self.assertEqual(self._ledger()["installed"], ["a.yaml", "c.yaml"])
        self.assertGreaterEqual(
            fired_any, 3,
            f"only {fired_any} failure points were reached -- the sweep proved too little",
        )

    def test_a_retraction_is_undone_when_a_later_rename_fails(self):
        self._install({"a.yaml": VALID, "b.yaml": VALID}, version="1.0.0")
        before = self._contents()
        # Drop b.yaml (a retraction) while adding c.yaml (two renames minimum).
        self._install_failing_on_nth_replace({"a.yaml": VALID, "c.yaml": VALID}, fail_after=1)
        self.assertTrue(
            (self.target / "b.yaml").exists(),
            "a retracted definition was not restored after a failed install",
        )
        self.assertEqual(self._contents(), before)

    def test_no_stray_files_after_a_midway_failure(self):
        self._install({"a.yaml": VALID}, version="1.0.0")
        for fail_after in range(0, 6):
            self._install_failing_on_nth_replace(
                {"a.yaml": VALID, "c.yaml": VALID}, fail_after
            )
        self.assertEqual(self._stray_files(), [], f"stray files: {self._stray_files()}")


class PermissionRetryTests(DefsInstallTransactionTests):
    """A transient lock must be ridden out, not surfaced as a failed update."""

    def test_a_transient_permission_error_is_retried(self):
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(source, destination):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "file is in use", str(destination))
            return real_replace(source, destination)

        with patch.object(defs_update.os, "replace", side_effect=flaky_replace), \
             patch.object(defs_update.time, "sleep"):
            result = self._install({"a.yaml": VALID})
        self.assertEqual(result["installed"], ["a.yaml"])
        self.assertGreater(calls["n"], 1, "the retry did not happen")

    def test_a_permanent_permission_error_still_raises_and_rolls_back(self):
        self._install({"a.yaml": VALID}, version="1.0.0")
        before = self._contents()
        archive = _pack({"a.yaml": VALID, "b.yaml": VALID})
        real_replace = os.replace
        state_file = defs_update.STATE_FILE

        def always_blocked(source, destination):
            if Path(destination).name == state_file:
                raise PermissionError(13, "file is in use", str(destination))
            return real_replace(source, destination)

        with patch.object(defs_update, "download_and_verify", return_value=archive), \
             patch.object(defs_update, "_index_version", return_value="2.0.0"), \
             patch.object(defs_update.os, "replace", side_effect=always_blocked), \
             patch.object(defs_update.time, "sleep"):
            with self.assertRaises(PermissionError):
                defs_update.install(data_dir=self.data_dir)
        self.assertEqual(self._contents(), before)


class ValidationStillPrecedesWritesTests(DefsInstallTransactionTests):
    def test_an_invalid_definition_writes_nothing(self):
        """Validation runs before staging, so a bad pack cannot half-install."""
        incomplete = VALID.replace("native_exe_windows: TestEmu.exe\n", "")
        self.assertNotIn("native_exe_windows", incomplete)
        archive = _pack({"a.yaml": VALID, "bad.yaml": incomplete})
        with patch.object(defs_update, "download_and_verify", return_value=archive), \
             patch.object(defs_update, "_index_version", return_value="2.0.0"):
            with self.assertRaises(defs_update.DefinitionError):
                defs_update.install(data_dir=self.data_dir)
        self.assertEqual(self._contents(), {}, "a rejected pack wrote a definition")
        self.assertEqual(self._stray_files(), [], f"stray files: {self._stray_files()}")


if __name__ == "__main__":
    unittest.main()
