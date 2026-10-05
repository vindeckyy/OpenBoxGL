#!/usr/bin/env python3
"""S16 -- the 10-second auto-import rewrote the whole state file every tick.

`update_with_result` always wrote: serialize the entire library, fsync it,
`shutil.copy2` the whole `library.json` to the backup, `os.replace` twice, fsync
the directory, and rotate a snapshot. There was no "did anything change" check.
The auto-import driver calls it every ten seconds on a default install with one
watch folder, and most of those ticks find nothing new -- so roughly 8,640 full
writes and backups a day, every one of them while holding the cross-process file
lock that every other state read has to acquire.

`OPENBOX_SNAPSHOT_DEBOUNCE` defaults to `0.0`, so the one existing early-return
never fired.

It also churned the state signature. The signature-keyed caches missed on every
browser refresh, and the plugin library hook re-ran per request -- so the cost
was not only disk I/O.

The invariant: **a mutation that changes nothing must not touch the disk.**
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import state_store


def _state(games=1):
    return {
        "schema_version": state_store.STATE_SCHEMA_VERSION,
        "games": [
            {"game_id": f"g{i}", "name": f"Game {i}", "path": f"/roms/{i}.zip", "platform": "NES"}
            for i in range(games)
        ],
        "profiles": {},
        "history": [],
        "settings": {},
        "playlists": [],
        "ui_state": {},
        "queue": [],
        "notifications": [],
    }


class NoOpWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "library.json"
        self.store = state_store.JsonStateStore(self.path, snapshot_limit=5, snapshot_debounce=0.0)
        self.store.save(_state(3))

    @contextlib.contextmanager
    def _count_disk_writes(self):
        """Count real writes: file writes, backup copies and snapshot rotations."""
        counts = {"write": 0, "copy2": 0, "rotate": 0}
        real_replace = os.replace
        real_copy2 = shutil.copy2
        real_rotate = self.store._rotate_snapshots

        def counting_replace(source, destination):
            if str(destination) == str(self.path):
                counts["write"] += 1
            return real_replace(source, destination)

        def counting_copy2(source, destination, **kwargs):
            counts["copy2"] += 1
            return real_copy2(source, destination, **kwargs)

        def counting_rotate(*args, **kwargs):
            counts["rotate"] += 1
            return real_rotate(*args, **kwargs)

        with mock.patch.object(state_store.os, "replace", side_effect=counting_replace), \
             mock.patch.object(state_store.shutil, "copy2", side_effect=counting_copy2), \
             mock.patch.object(self.store, "_rotate_snapshots", side_effect=counting_rotate):
            yield counts

    def _run_noop_ticks(self, count):
        with self._count_disk_writes() as counts:
            for _ in range(count):
                self.store.update(lambda state: None)
        return counts

    def test_a_no_op_mutation_touches_neither_the_file_nor_the_backup(self):
        counts = self._run_noop_ticks(20)
        self.assertEqual(
            counts["write"], 0,
            f"20 no-op mutations rewrote library.json {counts['write']} time(s)",
        )
        self.assertEqual(counts["copy2"], 0, "20 no-op mutations copied the backup 20 times")
        self.assertEqual(counts["rotate"], 0, "20 no-op mutations rotated 20 snapshots")

    def test_a_real_change_still_writes_exactly_once(self):
        def add_game(state):
            state["games"].append({"game_id": "new", "name": "New", "path": "/roms/n.zip"})

        with self._count_disk_writes() as counts:
            self.store.update(add_game)
        self.assertEqual(counts["write"], 1, "a real change did not reach the disk")
        self.assertEqual(counts["rotate"], 1)
        self.assertIn("New", self.path.read_text(encoding="utf-8"))

    def test_changing_then_unchanged_writes_only_once(self):
        with self._count_disk_writes() as counts:
            self.store.update(lambda s: s["settings"].update({"theme": "light"}))
            for _ in range(10):
                self.store.update(lambda s: None)
        self.assertEqual(counts["write"], 1, "the follow-up no-op ticks still wrote")

    def test_an_undoing_mutation_is_still_a_write(self):
        """A write that restores the previous content must persist it."""
        with self._count_disk_writes() as counts:
            self.store.update(lambda s: s["settings"].update({"theme": "light"}))
            self.store.update(lambda s: s["settings"].update({"theme": "dark"}))
        self.assertEqual(counts["write"], 2, "the state on disk no longer matches memory")

    def test_the_returned_state_is_still_correct_after_a_skip(self):
        state = self.store.update(lambda s: None)
        self.assertEqual(len(state["games"]), 3)
        self.assertEqual(state["schema_version"], state_store.STATE_SCHEMA_VERSION)

    def test_the_mutator_result_is_still_returned(self):
        sentinel = {"marker": ["a", 1]}
        _state_out, result = self.store.update_with_result(lambda s: sentinel)
        self.assertEqual(result, {"marker": ["a", 1]})

    def test_a_failed_mutation_still_raises_and_does_not_write(self):
        def boom(state):
            state["settings"]["theme"] = "half-applied"
            raise RuntimeError("mutator failed")

        with self.assertRaises(RuntimeError):
            self.store.update(boom)
        self.assertNotIn("half-applied", self.path.read_text(encoding="utf-8"))

    def test_a_subsequent_real_change_after_a_no_op_still_persists(self):
        for _ in range(5):
            self.store.update(lambda s: None)
        self.store.update(lambda s: s["settings"].update({"theme": "after"}))
        self.assertIn('"after"', self.path.read_text(encoding="utf-8"))

    def test_an_external_change_is_not_masked_by_the_digest(self):
        """Another process rewrites the file: the store must reload, not skip."""
        self.store.update(lambda s: None)
        state_store.normalize_state(_state(2))[0]["settings"]["theme"] = "external"
        self.store.save(_state(2))
        result = self.store.update(lambda s: None)
        self.assertEqual(
            len(result["games"]), 2,
            "the store served its cached copy after the file changed underneath it",
        )


class DigestTests(unittest.TestCase):
    def test_the_digest_ignores_key_order(self):
        left = state_store._state_digest({"a": 1, "b": 2})
        right = state_store._state_digest({"b": 2, "a": 1})
        self.assertEqual(left, right, "dict ordering changed the digest, missing every no-op")

    def test_the_digest_notices_a_value_change(self):
        self.assertNotEqual(
            state_store._state_digest({"games": [{"name": "A"}]}),
            state_store._state_digest({"games": [{"name": "B"}]}),
        )

    def test_the_digest_notices_a_nested_change(self):
        self.assertNotEqual(
            state_store._state_digest({"settings": {"theme": "a"}}),
            state_store._state_digest({"settings": {"theme": "b"}}),
        )

    def test_the_digest_tolerates_unserializable_values(self):
        """State can hold a datetime or a Path; normalization must not raise."""
        import datetime
        digest = state_store._state_digest({"when": datetime.datetime(2026, 1, 1)})
        self.assertIsInstance(digest, bytes)
        self.assertEqual(digest, state_store._state_digest({"when": datetime.datetime(2026, 1, 1)}))


class AutoImportBackoffTests(unittest.TestCase):
    """The driver should stop scanning on an idle install."""

    def _delays(self, found_per_tick, ticks=6):
        from pkg.state import imports as state_imports

        state = {
            "settings": {
                "watch_folders": ["/tmp/watch"],
                "storefront_auto_import": {},
                "emulator_scan_configs": [],
            }
        }
        waits = []
        counter = {"i": 0}

        class _Stop:
            def wait(self, _delay):
                waits.append(_delay)
                if len(waits) >= ticks:
                    return True
                return False

        def folder_import(_folder):
            added = found_per_tick[min(counter["i"], len(found_per_tick) - 1)]
            counter["i"] += 1
            return (added, 100, {})

        with mock.patch.object(state_imports, "load_state", return_value=state), \
             mock.patch.object(state_imports, "import_folder_path", side_effect=folder_import), \
             mock.patch.object(state_imports, "list_scan_configs", return_value=[]), \
             mock.patch.object(state_imports, "WATCH_STOP", _Stop()):
            state_imports.auto_import_worker()
        return waits

    def test_an_idle_install_backs_off(self):
        waits = self._delays([0])
        self.assertEqual(waits[0], 10, "the first tick should run at the floor")
        self.assertTrue(
            all(later >= waits[i] for i, later in enumerate(waits[1:])),
            f"an idle install did not back off: {waits}",
        )
        self.assertGreater(waits[-1], waits[0], f"an idle install never backed off: {waits}")

    def test_the_backoff_is_bounded(self):
        waits = self._delays([0], ticks=12)
        self.assertLessEqual(max(waits), 300, f"the backoff is unbounded: {waits}")

    def test_finding_something_returns_to_the_floor(self):
        """``waits[i]`` is the delay *before* tick i, so the productive tick's
        effect shows up on the next one."""
        waits = self._delays([0, 0, 5])
        self.assertEqual(waits[0], 10)
        self.assertGreater(waits[1], 10, "the idle tick did not back off")
        self.assertGreaterEqual(waits[2], waits[1], "the idle backoff did not keep growing")
        self.assertEqual(
            waits[3], 10,
            f"the tick after one that imported 5 games did not return to the 10s floor: {waits}",
        )
        self.assertTrue(all(delay == 10 for delay in waits[3:]), waits)


if __name__ == "__main__":
    unittest.main()
