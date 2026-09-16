#!/usr/bin/env python3
"""Regression tests for consistent state snapshot reads (ADR 0049).

Transactions mutate the cached state in place under the store thread lock.
Before 1.13.0 readers deep-copied the live object outside that lock, so a
concurrent request could observe a half-applied transaction.
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from state_store import JsonStateStore  # noqa: E402


class SnapshotConsistencyTests(unittest.TestCase):
    def test_read_snapshot_waits_for_in_flight_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save({
                "games": [{"game_id": "base", "name": "Base", "path": "/tmp/base"}],
                "profiles": {},
                "history": [],
                "settings": {},
                "playlists": [],
            })
            mutating = threading.Event()
            release = threading.Event()
            reading = threading.Event()

            def mutator(state):
                for index in range(25):
                    state["games"].append({
                        "game_id": f"g{index}", "name": f"G{index}", "path": "/tmp/g",
                    })
                mutating.set()
                release.wait(5)

            writer = threading.Thread(
                target=lambda: store.update_with_result(mutator, isolate=False),
                daemon=True,
            )
            writer.start()
            self.assertTrue(mutating.wait(5), "writer never reached the slow mutator")

            result = {}

            def reader():
                reading.set()
                result["snapshot"] = store.read_snapshot()

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()
            self.assertTrue(reading.wait(5))
            # Let the reader reach the lock while the transaction is paused.
            time.sleep(0.05)
            release.set()
            writer.join(5)
            reader_thread.join(5)

            self.assertIn("snapshot", result)
            state, signature = result["snapshot"]
            self.assertIsNotNone(signature)
            self.assertEqual(len(state["games"]), 26)

    def test_read_snapshot_returns_detached_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            first, _signature = store.read_snapshot()
            first["games"].append({"game_id": "mutated", "name": "Mutated"})
            second, _signature = store.read_snapshot()
            self.assertEqual(second["games"], [])


if __name__ == "__main__":
    unittest.main()
