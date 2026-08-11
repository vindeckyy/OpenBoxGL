#!/usr/bin/env python3

"""Focused contract tests for the schema v4 state migration and fast path."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from state_store import (
    NOTIFICATIONS_CAP,
    QUEUE_CAP,
    STATE_SCHEMA_VERSION,
    JsonStateStore,
    default_state,
    normalize_state,
)


def v3_state(**overrides):
    state = {
        "schema_version": 3,
        "games": [
            {"game_id": "game-aaa", "name": "Alpha", "path": "/tmp/alpha.rom"},
            {"game_id": "game-bbb", "name": "Beta", "path": "/tmp/beta.rom", "tags": ["RPG"]},
        ],
        "profiles": {"main": {"theme": "dark"}},
        "history": [{"game_id": "game-aaa", "played_at": "2026-01-01T00:00:00Z"}],
        "settings": {"volume": 80},
        "playlists": [{"id": "pl-1", "name": "Favorites", "games": ["game-aaa"]}],
        "custom_top_level": {"kept": True},
    }
    state.update(overrides)
    return state


class V3MigrationTests(unittest.TestCase):
    def test_v3_gains_collections_and_schema_4(self):
        state, changed = normalize_state(v3_state())
        self.assertTrue(changed)
        self.assertEqual(state["schema_version"], 4)
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["notifications"], [])

    def test_unknown_fields_survive_migration(self):
        state, _ = normalize_state(v3_state())
        self.assertEqual(state["custom_top_level"], {"kept": True})
        game = state["games"][0]
        self.assertEqual(game["name"], "Alpha")
        self.assertEqual(game["path"], "/tmp/alpha.rom")

    def test_missing_tags_stays_missing(self):
        state, _ = normalize_state(v3_state())
        self.assertNotIn("tags", state["games"][0])
        self.assertEqual(state["games"][1]["tags"], ["RPG"])

    def test_malformed_tags_becomes_empty_list(self):
        state = v3_state()
        state["games"][0]["tags"] = "RPG"
        state["games"][1]["tags"] = {"RPG": True}
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["games"][0]["tags"], [])
        self.assertEqual(normalized["games"][1]["tags"], [])

    def test_oversized_migrated_collections_are_capped(self):
        state = v3_state(
            queue=[{"id": f"qe-{i}"} for i in range(QUEUE_CAP + 25)],
            notifications=[{"id": f"nt-{i}"} for i in range(NOTIFICATIONS_CAP + 50)],
        )
        normalized, _ = normalize_state(state)
        self.assertEqual(len(normalized["queue"]), QUEUE_CAP)
        self.assertEqual(normalized["queue"][0]["id"], "qe-0")
        self.assertEqual(normalized["queue"][-1]["id"], f"qe-{QUEUE_CAP - 1}")
        self.assertEqual(len(normalized["notifications"]), NOTIFICATIONS_CAP)
        self.assertEqual(normalized["notifications"][0]["id"], "nt-0")

    def test_non_list_collections_replaced_with_empty(self):
        state = v3_state(queue="not-a-list", notifications=42)
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["queue"], [])
        self.assertEqual(normalized["notifications"], [])

    def test_none_collections_replaced_with_empty(self):
        state = v3_state(queue=None, notifications=None)
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["queue"], [])
        self.assertEqual(normalized["notifications"], [])


class FastPathTests(unittest.TestCase):
    def test_complete_v4_object_takes_fast_path(self):
        state = default_state()
        state["games"] = [{"game_id": "game-aaa", "name": "Alpha", "tags": []}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text(json.dumps(state))
            store = JsonStateStore(path)
            loaded = store.load()
            self.assertEqual(loaded, state)
            # Fast path must not rewrite the file: mtime/size unchanged after load.
            before = path.stat().st_mtime_ns
            store.load()
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_incomplete_v4_object_is_normalized(self):
        state = default_state()
        del state["queue"]
        del state["notifications"]
        state["games"] = [{"game_id": "game-aaa", "name": "Alpha"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text(json.dumps(state))
            store = JsonStateStore(path)
            loaded = store.load()
            self.assertEqual(loaded["queue"], [])
            self.assertEqual(loaded["notifications"], [])
            self.assertEqual(loaded["schema_version"], STATE_SCHEMA_VERSION)

    def test_fast_path_repairs_non_list_collections(self):
        state = default_state()
        state["queue"] = "oops"
        state["notifications"] = 7
        state["games"] = [{"game_id": "game-aaa", "name": "Alpha"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text(json.dumps(state))
            store = JsonStateStore(path)
            loaded = store.load()
            self.assertEqual(loaded["queue"], [])
            self.assertEqual(loaded["notifications"], [])
            persisted = json.loads(path.read_text())
            self.assertEqual(persisted["queue"], [])
            self.assertEqual(persisted["notifications"], [])

    def test_fast_path_repairs_non_list_game_tags(self):
        state = default_state()
        state["games"] = [
            {"game_id": "game-aaa", "name": "Alpha", "tags": "RPG"},
            {"game_id": "game-bbb", "name": "Beta"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text(json.dumps(state))
            store = JsonStateStore(path)
            loaded = store.load()
            self.assertEqual(loaded["games"][0]["tags"], [])
            self.assertNotIn("tags", loaded["games"][1])
            persisted = json.loads(path.read_text())
            self.assertEqual(persisted["games"][0]["tags"], [])

    def test_fast_path_repairs_oversized_collections(self):
        state = default_state()
        state["queue"] = [{"id": f"qe-{i}"} for i in range(QUEUE_CAP + 10)]
        state["notifications"] = [{"id": f"nt-{i}"} for i in range(NOTIFICATIONS_CAP + 10)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text(json.dumps(state))
            store = JsonStateStore(path)
            loaded = store.load()
            self.assertEqual(len(loaded["queue"]), QUEUE_CAP)
            self.assertEqual(len(loaded["notifications"]), NOTIFICATIONS_CAP)

    def test_defaults_and_repair_are_deterministic(self):
        raw = v3_state()
        first = normalize_state(copy.deepcopy(raw))
        second = normalize_state(copy.deepcopy(raw))
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        # Re-normalizing a normalized state is a stable no-op.
        third = normalize_state(copy.deepcopy(first[0]))
        self.assertEqual(third[0], first[0])
        self.assertFalse(third[1])


class DefaultStateTests(unittest.TestCase):
    def test_default_state_has_schema_4_collections(self):
        state = default_state()
        self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["notifications"], [])

    def test_store_save_normalizes_to_schema_4(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            saved = store.save({"games": [{"name": "Alpha"}], "settings": {}})
            self.assertEqual(saved["schema_version"], STATE_SCHEMA_VERSION)
            self.assertEqual(saved["queue"], [])
            self.assertEqual(saved["notifications"], [])
            loaded = store.load()
            self.assertTrue(loaded["games"][0]["game_id"])


if __name__ == "__main__":
    unittest.main()
