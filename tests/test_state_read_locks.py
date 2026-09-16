#!/usr/bin/env python3
"""P1-7: read paths take the shared file lock (LOCK_SH), writers stay LOCK_EX.

Before 1.13.0 ``_ensure_loaded``, ``load`` and ``load_readonly`` acquired the
exclusive lock, serializing every reader against every other reader.
"""

import fcntl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from state_store import JsonStateStore, default_state  # noqa: E402


def _seed(store):
    state = default_state()
    state["games"] = [{"game_id": "game-1", "name": "Doom", "path": "/tmp/doom"}]
    store.save(state)


def _flags(flock):
    return [call.args[1] for call in flock.call_args_list]


class ReadPathLockTests(unittest.TestCase):
    def _store(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return JsonStateStore(Path(tempdir.name) / "library.json")

    def test_load_uses_shared_lock_on_cache_miss(self):
        store = self._store()
        _seed(store)
        store._clear_cache()
        with mock.patch("state_store.fcntl.flock") as flock:
            state = store.load()
        self.assertEqual(state["games"][0]["game_id"], "game-1")
        flags = _flags(flock)
        self.assertIn(fcntl.LOCK_SH, flags)
        self.assertNotIn(fcntl.LOCK_EX, flags)

    def test_ensure_loaded_uses_shared_lock(self):
        store = self._store()
        _seed(store)
        store._clear_cache()
        with mock.patch("state_store.fcntl.flock") as flock:
            game = store.get_game_by_id("game-1")
        self.assertIsNotNone(game)
        flags = _flags(flock)
        self.assertIn(fcntl.LOCK_SH, flags)
        self.assertNotIn(fcntl.LOCK_EX, flags)

    def test_load_readonly_uses_shared_lock(self):
        store = self._store()
        _seed(store)
        store._clear_cache()
        with mock.patch("state_store.fcntl.flock") as flock:
            view = store.load_readonly()
        self.assertEqual(view["games"][0]["name"], "Doom")
        flags = _flags(flock)
        self.assertIn(fcntl.LOCK_SH, flags)
        self.assertNotIn(fcntl.LOCK_EX, flags)

    def test_write_paths_still_take_exclusive_lock(self):
        store = self._store()
        _seed(store)
        with mock.patch("state_store.fcntl.flock") as flock:
            store.update(lambda state: state["games"][0].__setitem__("favorite", True))
        self.assertIn(fcntl.LOCK_EX, _flags(flock))

    def test_reader_completes_while_another_fd_holds_shared_lock(self):
        store = self._store()
        _seed(store)
        store._clear_cache()
        holding = threading.Event()
        release = threading.Event()

        def holder():
            with store.lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                holding.set()
                release.wait(10)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(holding.wait(5), "lock holder never started")
        loaded = {}
        finished = threading.Event()

        def reader():
            try:
                loaded["state"] = store.load()
            finally:
                finished.set()

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        try:
            completed = finished.wait(3)
        finally:
            release.set()
            thread.join(10)
            reader_thread.join(10)
        self.assertTrue(completed, "reader blocked behind a shared lock held elsewhere")
        self.assertEqual(loaded["state"]["games"][0]["name"], "Doom")


if __name__ == "__main__":
    unittest.main()
