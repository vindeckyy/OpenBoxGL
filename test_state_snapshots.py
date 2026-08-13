"""State store hardening: snapshot rotation and dry-run recovery."""

import unittest
import tempfile
from pathlib import Path

from state_store import JsonStateStore


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

    def test_unknown_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save({"games": []})
            from state_store import StateCorruptError
            with self.assertRaises(StateCorruptError):
                store.restore_snapshot("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
