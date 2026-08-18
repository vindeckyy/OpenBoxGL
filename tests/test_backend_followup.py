#!/usr/bin/env python3

import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from archives import safe_zip_extract
from job_manager import JobManager
from state_store import JsonStateStore


def write_setting(path, key):
    JsonStateStore(Path(path)).update(lambda state: state.setdefault("settings", {}).__setitem__(key, True))


class BackendFollowupTests(unittest.TestCase):
    def test_ids_survive_reordering_and_legacy_ids_remain_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            legacy = "game-0123456789abcdef01234567-0"
            store.save({
                "schema_version": 2,
                "games": [
                    {"game_id": legacy, "name": "A", "path": "/tmp/a.rom"},
                    {"name": "B", "path": "/tmp/b.rom"},
                ],
            })
            first = store.load()
            ids = {game["name"]: game["game_id"] for game in first["games"]}
            self.assertIn(legacy, first["games"][0]["legacy_game_ids"])
            store.update(lambda state: state.update({"games": list(reversed(state["games"]))}))
            second = store.load()
            self.assertEqual(ids, {game["name"]: game["game_id"] for game in second["games"]})

    def test_loaded_state_isolation_and_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save({"games": []})
            loaded = store.load()
            loaded["settings"]["unsaved"] = True
            self.assertNotIn("unsaved", store.load()["settings"])
            threads = [
                threading.Thread(target=write_setting, args=(str(path), f"writer_{index}"))
                for index in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            state = store.load()
            self.assertEqual(
                {f"writer_{index}" for index in range(12)},
                {key for key in state["settings"] if key.startswith("writer_")},
            )

    def test_job_retry_and_cancellation(self):
        manager = JobManager(max_workers=1)
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise RuntimeError("temporary")
            return {"value": "ok"}

        with mock.patch("job_manager.LOGGER.exception"):
            manager.submit("retry", flaky, max_attempts=2, backoff_seconds=0)
        for _ in range(100):
            if manager.snapshot("retry").get("state") in {"done", "error"}:
                break
            time.sleep(0.01)
        self.assertEqual(manager.snapshot("retry").get("state"), "done")
        self.assertEqual(attempts["count"], 2)

        started = threading.Event()

        def cancellable(cancel_event):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.005)

        manager.submit("cancel", cancellable)
        self.assertTrue(started.wait(1))
        self.assertTrue(manager.cancel("cancel"))
        for _ in range(100):
            if manager.snapshot("cancel").get("state") == "cancelled":
                break
            time.sleep(0.01)
        self.assertEqual(manager.snapshot("cancel").get("state"), "cancelled")
        manager.shutdown(wait=True, cancel_futures=True)

    def test_completed_futures_are_released(self):
        manager = JobManager(max_workers=2)
        for index in range(5):
            manager.submit(f"job-{index}", lambda: {"ok": True})
        for _ in range(200):
            if all(manager.snapshot(f"job-{index}").get("state") == "done" for index in range(5)) and not manager._futures:
                break
            time.sleep(0.01)
        self.assertEqual(manager._futures, {}, "completed futures must not accumulate")
        manager.shutdown(wait=True, cancel_futures=True)

    def test_job_queue_and_name_limits(self):
        manager = JobManager(max_workers=1, max_jobs=1)
        started = threading.Event()
        release = threading.Event()

        def blocked(cancel_event):
            started.set()
            release.wait(1)

        try:
            manager.submit("first", blocked)
            self.assertTrue(started.wait(1))
            with self.assertRaises(RuntimeError):
                manager.submit("second", lambda: None)
            with self.assertRaises(RuntimeError):
                manager.submit("first", lambda: None, replace=True)
            self.assertEqual(manager.snapshot("first").get("state"), "running")
            with self.assertRaises(ValueError):
                manager.submit("x" * 129, lambda: None)
        finally:
            release.set()
            manager.shutdown(wait=True, cancel_futures=True)

    def test_job_results_are_bounded(self):
        manager = JobManager(max_workers=1)
        try:
            manager.submit("large-result", lambda: {"blob": "x" * 70_000})
            for _ in range(100):
                if manager.snapshot("large-result").get("state") == "done":
                    break
                time.sleep(0.01)
            result = manager.snapshot("large-result")
            self.assertTrue(result.get("result_truncated"))
            self.assertNotIn("blob", result)
        finally:
            manager.shutdown(wait=True, cancel_futures=True)

    def test_archive_duplicate_and_size_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "duplicate.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as package:
                    package.writestr("game.rom", b"one")
                    package.writestr("game.rom", b"two")
            with self.assertRaises(ValueError):
                safe_zip_extract(duplicate, root / "out")

            oversized = root / "oversized.zip"
            with zipfile.ZipFile(oversized, "w") as package:
                package.writestr("game.rom", b"12345")
            with self.assertRaises(ValueError):
                safe_zip_extract(oversized, root / "out2", max_member_bytes=4)


if __name__ == "__main__":
    unittest.main()
