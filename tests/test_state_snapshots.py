"""State store hardening: snapshot rotation and dry-run recovery."""

import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state_store import JsonStateStore, StateCorruptError, default_state


class SnapshotTests(unittest.TestCase):
    def test_snapshots_rotate_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=5)
            store.save({"games": [{"name": "one"}]})
            store.update(lambda s: s["games"].append({"name": "two"}))
            store.update(lambda s: s["games"].append({"name": "three"}))
            self.assertEqual(len(store.snapshots()), 3)
            # a bad mutation lands on top of the last good snapshot; restore
            # the snapshot from before it
            store.update(lambda s: s["games"].append({"name": "oops"}))
            newest = store.snapshots()[1]["name"]
            state = store.restore_snapshot(newest)
            self.assertEqual([g["name"] for g in state["games"]], ["one", "two", "three"])

    def test_snapshot_names_are_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save({"games": []})
            self.assertEqual(len(store.snapshots()), 1)

    def test_snapshots_are_ordered_by_name_not_by_filesystem_mtime(self):
        """A snapshot is a hard link to the live file, so mtime is not a sort key.

        Two writes landing inside one filesystem timestamp tick produced two
        snapshots the ordering could not distinguish, "newest first" became
        arbitrary, and restoring the snapshot from before a bad write restored
        the wrong one. This reproduced roughly once in twelve runs. Ordering is
        now a property of the filename.
        """
        import os

        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=5)
            store.save({"games": [{"name": "one"}]})
            for name in ("two", "three", "oops"):
                store.update(lambda state, n=name: state["games"].append({"name": n}))

            # Force every snapshot to report the same mtime: the hard links
            # already share the live file's inode.
            fixed = 1_000_000.0
            for path in store.snapshots_dir.glob("*.json"):
                os.utime(path, (fixed, fixed))

            listed = store.snapshots()
            self.assertEqual(len(listed), 4)
            restored = store.restore_snapshot(listed[1]["name"])
            self.assertEqual(
                [game["name"] for game in restored["games"]], ["one", "two", "three"],
                "with equal mtimes the snapshot order became arbitrary",
            )

    def test_a_snapshot_name_from_the_old_format_still_lists(self):
        """Old snapshot directories must not break or lose entries."""
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=5)
            store.save({"games": [{"name": "one"}]})
            store.update(lambda state: state["games"].append({"name": "two"}))
            legacy = store.snapshots_dir / "20200101T000000Z-abcd1234.json"
            legacy.write_text('{"games": [{"name": "legacy"}]}', encoding="utf-8")

            listed = store.snapshots()
            self.assertEqual(len(listed), 3, "a legacy snapshot name was dropped")
            names = [item["name"] for item in listed]
            self.assertIn(legacy.name, names)
            # Named (new-format) snapshots sort ahead of the legacy one.
            self.assertEqual(names[-1], legacy.name)

    def test_unknown_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save({"games": []})
            from state_store import StateCorruptError
            with self.assertRaises(StateCorruptError):
                store.restore_snapshot("../../etc/passwd")

    def test_recover_restores_backup_after_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            store.update(lambda state: state["settings"].update({"saved": True}))
            store.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                store.load()
            recovered = store.recover()
            self.assertTrue(recovered["settings"]["saved"])

    def test_corrupt_primary_raises_without_clobbering_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            backup_before = store.backup_path.read_text(encoding="utf-8")
            store.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                store.load()
            self.assertEqual(store.backup_path.read_text(encoding="utf-8"), backup_before)

    def test_snapshot_debounce_skips_extra_rotations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(
                Path(directory) / "library.json",
                snapshot_limit=5,
                snapshot_debounce=3600.0,
            )
            store.save(default_state())
            store.update(lambda state: state["settings"].update({"n": 1}))
            self.assertEqual(len(store.snapshots()), 1)

    def test_recover_without_backup_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            with self.assertRaises(StateCorruptError):
                store.recover()


if __name__ == "__main__":
    unittest.main()
