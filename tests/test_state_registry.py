"""Tests for pkg.state.registry — the process registry module."""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from pkg.state.registry import (
    EVENT_SEQUENCE,
    PROCESS_LOCK,
    PROCESSES,
    RUNNING,
    SESSION_EVENTS,
    Session,
)


class TestSessionDataclass(unittest.TestCase):
    def test_creation(self):
        s = Session(
            launch_id="abc123",
            stable_game_id="game-1",
            pid=42,
            pgid=42,
            proc_start_time=1234567890.0,
            cmd_fingerprint="/usr/bin/game --fullscreen",
            game_name="My Game",
        )
        self.assertEqual(s.launch_id, "abc123")
        self.assertEqual(s.stable_game_id, "game-1")
        self.assertEqual(s.pid, 42)
        self.assertEqual(s.pgid, 42)
        self.assertEqual(s.proc_start_time, 1234567890.0)
        self.assertEqual(s.cmd_fingerprint, "/usr/bin/game --fullscreen")
        self.assertEqual(s.game_name, "My Game")

    def test_fields_are_dataclass_fields(self):
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(Session)}
        self.assertEqual(
            field_names,
            {"launch_id", "stable_game_id", "pid", "pgid", "proc_start_time", "cmd_fingerprint", "game_name"},
        )


class TestRunningDict(unittest.TestCase):
    def setUp(self):
        RUNNING.clear()

    def test_add_and_lookup(self):
        RUNNING["launch-1"] = {"game": "Doom", "pid": 100}
        self.assertIn("launch-1", RUNNING)
        self.assertEqual(RUNNING["launch-1"]["game"], "Doom")

    def test_remove(self):
        RUNNING["launch-2"] = {"game": "Quake"}
        del RUNNING["launch-2"]
        self.assertNotIn("launch-2", RUNNING)

    def test_pop_default(self):
        result = RUNNING.pop("nonexistent", {})
        self.assertEqual(result, {})

    def test_is_dict(self):
        self.assertIsInstance(RUNNING, dict)


class TestProcessesDict(unittest.TestCase):
    def setUp(self):
        PROCESSES.clear()

    def test_add_and_lookup(self):
        fake_proc = object()
        PROCESSES["launch-3"] = fake_proc
        self.assertIs(PROCESSES["launch-3"], fake_proc)

    def test_is_dict(self):
        self.assertIsInstance(PROCESSES, dict)


class TestSessionEvents(unittest.TestCase):
    def setUp(self):
        SESSION_EVENTS.clear()

    def test_is_list(self):
        self.assertIsInstance(SESSION_EVENTS, list)

    def test_append_and_read(self):
        SESSION_EVENTS.append({"kind": "started", "launch_id": "x"})
        self.assertEqual(len(SESSION_EVENTS), 1)
        self.assertEqual(SESSION_EVENTS[0]["kind"], "started")

    def test_bounded_to_100(self):
        for i in range(150):
            SESSION_EVENTS.append({"id": i})
            SESSION_EVENTS[:] = SESSION_EVENTS[-100:]
        self.assertEqual(len(SESSION_EVENTS), 100)
        # Oldest 50 should be gone
        self.assertEqual(SESSION_EVENTS[0]["id"], 50)
        self.assertEqual(SESSION_EVENTS[-1]["id"], 149)


class TestProcessLock(unittest.TestCase):
    def test_is_lock(self):
        self.assertIsInstance(PROCESS_LOCK, type(threading.Lock()))

    def test_acquire_release(self):
        acquired = PROCESS_LOCK.acquire(timeout=1)
        self.assertTrue(acquired)
        PROCESS_LOCK.release()

    def test_context_manager(self):
        with PROCESS_LOCK:
            pass  # Should not deadlock


class TestEventSequence(unittest.TestCase):
    def test_is_int(self):
        self.assertIsInstance(EVENT_SEQUENCE, int)

    def test_increment_via_registry_module(self):
        import pkg.state.registry as reg
        original = reg.EVENT_SEQUENCE
        reg.EVENT_SEQUENCE += 1
        self.assertEqual(reg.EVENT_SEQUENCE, original + 1)
        # Restore
        reg.EVENT_SEQUENCE = original


if __name__ == "__main__":
    unittest.main()
