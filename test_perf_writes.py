"""Regression tests for the state-store write path (Phase 3).

Verifies: single backup copy per commit, no redundant normalization,
compact serialization for large libraries, preserved crash-safety ordering,
and that returned state is the committed snapshot.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from state_store import JsonStateStore


class PerfWriteTests(unittest.TestCase):
    def make_store(self, directory):
        return JsonStateStore(Path(directory) / "library.json")

    def test_single_backup_copy_per_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            with mock.patch("shutil.copy2", wraps=__import__("shutil").copy2) as copy2:
                store.save({"games": [{"game_id": "g1", "name": "One"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            backup_copies = [call for call in copy2.call_args_list if "library.json.bak" in str(call)]
            self.assertEqual(len(backup_copies), 1, "backup must be written exactly once per commit")

    def test_backup_matches_primary_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "First"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            store.save({"games": [{"game_id": "g1", "name": "Second"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            primary = json.loads(store.path.read_text())
            backup = json.loads(store.backup_path.read_text())
            self.assertEqual(primary["games"][0]["name"], "Second")
            self.assertEqual(backup["games"][0]["name"], "Second")

    def test_large_library_writes_compact_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            games = [{"game_id": f"g{i}", "name": f"Game {i}", "description": "x" * 200} for i in range(20000)]
            state = {"games": games, "profiles": {}, "history": [], "settings": {}, "playlists": []}
            store.save(state)
            raw = store.path.read_text()
            self.assertNotIn("\n  \"game_id\"", raw, "large libraries must be serialized compactly")
            self.assertGreater(store.path.stat().st_size, 1024 * 1024)
            loaded = json.loads(raw)
            self.assertEqual(len(loaded["games"]), 20000)

    def test_small_library_stays_pretty(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            state = {"games": [{"game_id": "g1", "name": "One"}], "profiles": {}, "history": [], "settings": {}, "playlists": []}
            store.save(state)
            self.assertIn("\n      \"game_id\"", store.path.read_text())

    def test_update_returns_committed_state_by_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            state, result = store.update_with_result(
                lambda current: current["games"].append({"game_id": "g1", "name": "One"})
            )
            self.assertEqual(result, None)
            self.assertEqual([game["name"] for game in state["games"]], ["One"])
            reloaded = store.load()
            self.assertEqual(reloaded["games"][0]["name"], "One", "committed state must be persisted")

    def test_update_returns_detached_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            state = store.update(
                lambda current: current["games"].append({"game_id": "g1", "name": "One"})
            )
            self.assertEqual([game["name"] for game in state["games"]], ["One"])
            state["games"][0]["name"] = "Mutated"
            reloaded = store.load()
            self.assertEqual(reloaded["games"][0]["name"], "One", "returned state must not alias stored state")

    def test_write_failure_keeps_primary_and_backup_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "Before"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            with mock.patch("os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    store.save({"games": [{"game_id": "g1", "name": "After"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            primary = json.loads(store.path.read_text())
            backup = json.loads(store.backup_path.read_text())
            self.assertEqual(primary["games"][0]["name"], "Before")
            self.assertIn(backup["games"][0]["name"], {"Before", "After"})

    def test_recover_still_restores_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "Good"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            store.path.write_text("{corrupt", encoding="utf-8")
            recovered = store.recover()
            self.assertEqual(recovered["games"][0]["name"], "Good")
            self.assertEqual(json.loads(store.path.read_text())["games"][0]["name"], "Good")


if __name__ == "__main__":
    unittest.main()
