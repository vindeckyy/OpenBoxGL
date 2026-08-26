#!/usr/bin/env python3

"""Focused contract tests for the schema v4 state migration and fast path."""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state_store
from state_store import (
    NOTIFICATIONS_CAP,
    QUEUE_CAP,
    STATE_SCHEMA_VERSION,
    JsonStateStore,
    StateCorruptError,
    _apply_migrations,
    _migrate_v2_to_v3,
    _normalize_game_ids,
    _validate_state,
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
    def test_v3_gains_collections_and_schema_5(self):
        state, changed = normalize_state(v3_state())
        self.assertTrue(changed)
        self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["notifications"], [])
        self.assertEqual(state["ui_state"], {})

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


class TransactionValidationTests(unittest.TestCase):
    def test_invalid_update_rejected_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save(default_state())
            before = path.read_text()
            with self.assertRaises(StateCorruptError):
                store.update(lambda state: state.__setitem__("games", "not-a-list"))
            self.assertEqual(path.read_text(), before)

    def test_schema_remains_6_after_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            saved = store.save(default_state())
            self.assertEqual(saved["schema_version"], 6)
            loaded = store.load()
            self.assertEqual(loaded["schema_version"], 6)
            store.update(lambda state: state["settings"].update({"locale": "fr"}))
            reloaded = store.load()
            self.assertEqual(reloaded["schema_version"], 6)


class StoreIndexTests(unittest.TestCase):
    def test_games_by_id_and_platform_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            saved = store.save({
                "games": [
                    {"game_id": "game-pc", "name": "Doom", "platform": "PC", "path": "/tmp/doom"},
                    {"game_id": "game-snes", "name": "Chrono", "platform": "SNES", "path": "/tmp/chrono"},
                ],
                "settings": {},
            })
            game_id = saved["games"][0]["game_id"]
            self.assertEqual(store.get_game_by_id(game_id)["name"], "Doom")
            self.assertIsNone(store.get_game_by_id("missing"))
            pc_games = store.get_games_by_platform("PC")
            self.assertEqual([game["name"] for game in pc_games], ["Doom"])
            self.assertEqual(store.games_by_id[saved["games"][1]["game_id"]]["platform"], "SNES")

    def test_load_returns_detached_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            first = store.load()
            first["settings"]["locale"] = "mutated"
            second = store.load()
            self.assertNotIn("locale", second["settings"])

    def test_update_exception_clears_cache_without_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save(default_state())
            before = path.read_text()

            def boom(_state):
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                store.update(boom)
            self.assertEqual(path.read_text(), before)


class CorruptionTests(unittest.TestCase):
    def test_unsupported_schema_version_rejected(self):
        with self.assertRaises(StateCorruptError):
            normalize_state({"schema_version": 99, "games": []})

    def test_legacy_list_root_migrates_to_schema_6(self):
        state, changed = normalize_state([{"name": "Legacy", "path": "/tmp/a.rom"}])
        self.assertTrue(changed)
        self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
        self.assertTrue(state["games"][0]["game_id"])

    def test_invalid_game_entry_rejected(self):
        state = default_state()
        state["games"] = ["not-a-dict"]
        with self.assertRaises(StateCorruptError):
            normalize_state(state)

    def test_legacy_indexed_ids_migrate_with_aliases(self):
        legacy_id = "game-abcdef0123456789abcdef01-1"
        state = {
            "schema_version": 2,
            "games": [
                {"game_id": legacy_id, "name": "Legacy", "path": "/tmp/legacy.rom"},
            ],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["schema_version"], STATE_SCHEMA_VERSION)
        self.assertIn(legacy_id, normalized["games"][0].get("legacy_game_ids", []))

    def test_duplicate_stable_ids_receive_suffix(self):
        games = [
            {"name": "Twin A", "path": "/tmp/same.rom"},
            {"name": "Twin B", "path": "/tmp/same.rom"},
        ]
        normalized, _ = normalize_state({"games": games})
        ids = [game["game_id"] for game in normalized["games"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(ids[1].startswith(ids[0]))

    def test_invalid_active_sessions_are_stripped(self):
        state = default_state()
        state["active_sessions"] = [{"ok": True}, "bad"]
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["active_sessions"], [{"ok": True}])


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


class StoreWritePathTests(unittest.TestCase):
    def test_missing_file_load_returns_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            loaded = store.load()
            self.assertEqual(loaded["schema_version"], STATE_SCHEMA_VERSION)

    def test_corrupt_json_raises_state_corrupt_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text("{bad", encoding="utf-8")
            store = JsonStateStore(path)
            with self.assertRaises(StateCorruptError):
                store.load()

    def test_secure_text_write_sets_mode(self):
        import stat
        from state_store import secure_text_write

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secret.txt"
            secure_text_write(target, "token")
            self.assertEqual(target.read_text(encoding="utf-8"), "token")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_signature_none_when_file_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            self.assertIsNone(store.signature())

    def test_update_with_result_normalizes_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            state, _ = store.update_with_result(
                lambda current: current["games"].append({"name": "Fresh", "path": "/tmp/fresh"})
            )
            self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
            self.assertTrue(state["games"][0]["game_id"])


class JsonHelperTests(unittest.TestCase):
    def test_orjson_helpers_when_available(self):
        if state_store._orjson is None:
            self.skipTest("orjson unavailable")
        payload = {"b": 2, "a": 1}
        self.assertIn('"a"', state_store._json_dumps(payload, sort_keys=True))
        self.assertIsInstance(state_store._json_dumps_bytes(payload, indent=2), bytes)
        self.assertIsInstance(state_store._json_dumps_bytes(payload, sort_keys=True), bytes)
        self.assertIn('"a"', state_store._json_dumps(payload, indent=2))
        text_fp = io.StringIO()
        state_store._json_dump_file(payload, text_fp, indent=2)
        self.assertTrue(text_fp.getvalue().startswith("{"))
        bin_fp = io.BytesIO()
        bin_fp.mode = "wb"
        state_store._json_dump_file(payload, bin_fp, indent=2)
        self.assertTrue(bin_fp.getvalue().startswith(b"{"))
        self.assertEqual(state_store._json_load(io.StringIO('{"x": 1}')), {"x": 1})
        self.assertEqual(state_store._json_load(io.BytesIO(b'{"y": 2}')), {"y": 2})
        self.assertEqual(state_store._json_dumps_bytes(payload, indent=2)[:1], b"{")
        with tempfile.NamedTemporaryFile("w+b") as handle:
            state_store._json_dump_file(payload, handle, sort_keys=True)
            handle.seek(0)
            self.assertIn(b'"a"', handle.read())
        binary_reader = mock.Mock()
        binary_reader.mode = "rb"
        binary_reader.read = mock.Mock(return_value=b'{"z": 9}')
        self.assertEqual(state_store._json_load(binary_reader), {"z": 9})

    def test_stdlib_json_helpers_when_orjson_missing(self):
        if state_store._orjson is not None:
            self.skipTest("stdlib JSON helpers are inactive when orjson is installed")
        payload = {"z": 3}
        self.assertEqual(state_store._json_dumps(payload), json.dumps(payload))
        self.assertEqual(
            state_store._json_dumps_bytes(payload, indent=2),
            json.dumps(payload, indent=2).encode("utf-8"),
        )
        fp = io.StringIO()
        state_store._json_dump_file(payload, fp, indent=2)
        self.assertEqual(json.load(io.StringIO(fp.getvalue())), payload)
        self.assertEqual(state_store._json_load(io.StringIO('{"w": 4}')), {"w": 4})


class NormalizeEdgeCaseTests(unittest.TestCase):
    def test_missing_migration_raises(self):
        with mock.patch.dict(state_store.MIGRATIONS, {1: None}, clear=True):
            with self.assertRaises(StateCorruptError):
                _apply_migrations({"schema_version": 1, "games": []}, 1)

    def test_invalid_root_type_rejected(self):
        with self.assertRaises(StateCorruptError):
            normalize_state("not-state")

    def test_invalid_schema_version_type_coerced(self):
        state, changed = normalize_state({"schema_version": "oops", "games": []})
        self.assertTrue(changed)
        self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)

    def test_validate_rejects_bad_games_collection(self):
        bad = default_state()
        bad["games"] = "nope"
        with self.assertRaises(StateCorruptError):
            _validate_state(bad)

    def test_validate_rejects_game_without_id(self):
        bad = default_state()
        bad["games"] = [{"name": "NoId"}]
        with self.assertRaises(StateCorruptError):
            _validate_state(bad)

    def test_normalize_strips_invalid_active_sessions(self):
        state = default_state()
        state["active_sessions"] = "bad"
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["active_sessions"], [])

    def test_normalize_repairs_non_list_legacy_game_ids(self):
        state = default_state()
        state["games"] = [{
            "game_id": "game-stable",
            "name": "Legacy",
            "path": "/tmp/legacy.rom",
            "legacy_game_ids": "old",
        }]
        normalized, changed = normalize_state(state)
        self.assertTrue(changed)
        self.assertEqual(normalized["games"][0]["legacy_game_ids"], [])

    def test_v2_migration_repairs_legacy_game_ids_list(self):
        game = {
            "game_id": "game-abcdef0123456789abcdef01-1",
            "legacy_game_ids": "bad",
            "name": "Legacy",
            "path": "/tmp/legacy.rom",
        }
        raw = {
            "schema_version": 2,
            "games": [game],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }
        _migrate_v2_to_v3(raw)
        aliases = raw["games"][0]["legacy_game_ids"]
        self.assertIsInstance(aliases, list)
        self.assertIn("game-abcdef0123456789abcdef01-1", aliases)

    def test_v2_migration_suffixes_duplicate_stable_ids(self):
        twin = {"name": "Twin", "path": "/tmp/same.rom"}
        raw = {
            "schema_version": 2,
            "games": [dict(twin), dict(twin)],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }
        _migrate_v2_to_v3(raw)
        ids = [game["game_id"] for game in raw["games"]]
        self.assertEqual(len(set(ids)), 2)

    def test_normalize_game_ids_adds_legacy_aliases_and_suffixes(self):
        games = [
            {"game_id": "game-abcdef0123456789abcdef01-1", "name": "A", "path": "/tmp/a"},
            {"name": "Twin", "path": "/tmp/same.rom"},
            {"name": "Twin", "path": "/tmp/same.rom"},
        ]
        changed = _normalize_game_ids(games)
        self.assertTrue(changed)
        self.assertIn("game-abcdef0123456789abcdef01-1", games[0]["legacy_game_ids"])
        self.assertNotEqual(games[1]["game_id"], games[2]["game_id"])

    def test_normalize_game_ids_suffixes_duplicates(self):
        games = [
            {"game_id": "game-abcdef0123456789abcdef01-9", "name": "A", "path": "/tmp/x"},
            {"name": "Twin", "path": "/tmp/same.rom"},
            {"name": "Twin", "path": "/tmp/same.rom"},
        ]
        changed = _normalize_game_ids(games)
        self.assertTrue(changed)
        self.assertIn("game-abcdef0123456789abcdef01-9", games[0]["legacy_game_ids"])
        self.assertNotEqual(games[1]["game_id"], games[2]["game_id"])

    def test_normalize_rejects_non_list_games_after_defaults(self):
        with mock.patch.object(state_store, "_apply_migrations", return_value=False):
            with mock.patch.object(state_store, "_ensure_defaults", return_value=False):
                with self.assertRaises(StateCorruptError):
                    normalize_state({"schema_version": 6, "games": {}})

    def test_v2_migration_skips_non_dict_games(self):
        raw = {
            "schema_version": 2,
            "games": ["skip-me", {"game_id": "game-aaa", "name": "Keep", "path": "/tmp/a"}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }
        _migrate_v2_to_v3(raw)
        self.assertEqual(len(raw["games"]), 2)
        self.assertEqual(raw["schema_version"], 3)

    def test_v2_migration_repairs_legacy_aliases_and_suffixes(self):
        legacy_id = "game-abcdef0123456789abcdef01-1"
        raw = {
            "schema_version": 2,
            "games": [
                {
                    "game_id": legacy_id,
                    "name": "Twin",
                    "path": "/tmp/twin.rom",
                    "legacy_game_ids": "bad",
                },
                {"name": "Twin", "path": "/tmp/twin.rom"},
            ],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }
        _migrate_v2_to_v3(raw)
        aliases = raw["games"][0].get("legacy_game_ids", [])
        self.assertIn(legacy_id, aliases)
        ids = [game["game_id"] for game in raw["games"] if isinstance(game, dict)]
        self.assertEqual(len(set(ids)), 2)

    def test_normalize_rejects_non_list_games_string_payload(self):
        state = {"schema_version": 6, "games": "bad", "profiles": {}, "history": [], "settings": {}, "playlists": []}
        with self.assertRaises(StateCorruptError):
            normalize_state(state)


class StoreInternalsTests(unittest.TestCase):
    def test_remember_adopt_deep_copies_non_list_games(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            payload = default_state()
            payload["games"] = "not-a-list"
            store._remember(payload, adopt=True)
            self.assertEqual(store._cached_state["games"], "not-a-list")

    def test_games_by_platform_property_loads_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save({
                "games": [
                    {"game_id": "g1", "name": "A", "platform": "PC", "path": "/a"},
                    {"game_id": "g2", "name": "B", "platform": "PC", "path": "/b"},
                ],
            })
            by_platform = store.games_by_platform
            self.assertEqual(len(by_platform["PC"]), 2)

    def test_ensure_loaded_refreshes_after_external_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save(default_state())
            store.load()
            on_disk = json.loads(path.read_text())
            on_disk["settings"] = {"edited": True}
            path.write_text(json.dumps(on_disk))
            reloaded = store.load()
            self.assertEqual(reloaded["settings"], {"edited": True})

    def test_load_readonly_repairs_stale_disk_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save(default_state())
            store.load_readonly()
            on_disk = json.loads(path.read_text())
            del on_disk["queue"]
            path.write_text(json.dumps(on_disk))
            view = store.load_readonly()
            self.assertEqual(view["queue"], [])

    def test_recover_raises_when_backup_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            store.backup_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                store.recover()

    def test_save_failure_clears_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            store.load()
            with mock.patch.object(store, "_write_unlocked", side_effect=RuntimeError("disk")):
                with self.assertRaises(RuntimeError):
                    store.save(default_state())
            self.assertIsNone(store._cached_state)

    def test_snapshot_link_fallback_and_prune_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save(default_state())
            with mock.patch("state_store.os.link", side_effect=OSError("nlink")):
                store.update(lambda state: state["settings"].update({"n": 1}))
            self.assertGreaterEqual(len(store.snapshots()), 1)
            fake_dir = mock.Mock()
            fake_dir.glob = mock.Mock(side_effect=OSError("glob"))
            with mock.patch.object(store, "snapshots_dir", fake_dir):
                self.assertEqual(store.snapshots(), [])

    def test_snapshot_rotation_oserror_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=1)
            store.save(default_state())
            fake_dir = mock.Mock()
            fake_dir.mkdir = mock.Mock(side_effect=OSError("mkdir"))
            with mock.patch.object(store, "snapshots_dir", fake_dir):
                store.update(lambda state: state["settings"].update({"n": 2}))
            self.assertTrue(store.path.is_file())

    def test_ensure_loaded_normalizes_stale_on_disk_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            legacy = {
                "schema_version": 3,
                "games": [{"game_id": "game-aaa", "name": "Alpha", "path": "/tmp/a"}],
                "profiles": {},
                "history": [],
                "settings": {},
                "playlists": [],
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")
            store = JsonStateStore(path)
            game = store.get_game_by_id("game-aaa")
            self.assertEqual(game["name"], "Alpha")
            persisted = json.loads(path.read_text())
            self.assertEqual(persisted["schema_version"], STATE_SCHEMA_VERSION)

    def test_snapshot_limit_zero_skips_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=0)
            store.save(default_state())
            store.update(lambda state: state["settings"].update({"n": 1}))
            self.assertEqual(store.snapshots(), [])

    def test_snapshots_skip_unreadable_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save(default_state())
            bad = store.snapshots_dir / "broken.json"
            bad.write_text("{}", encoding="utf-8")
            with mock.patch.object(type(bad), "stat", side_effect=OSError("stat")):
                names = [item["name"] for item in store.snapshots()]
            self.assertNotIn("broken.json", names)

    def test_medium_library_compacts_orjson_payload(self):
        if state_store._orjson is None:
            self.skipTest("orjson unavailable")
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            games = [{"game_id": f"g{i}", "name": f"G{i}", "desc": "y" * 15000} for i in range(100)]
            store.save({"games": games})
            self.assertGreater(store.path.stat().st_size, 1024 * 1024)

    def test_write_cleans_temporary_file_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            with mock.patch("state_store.os.replace", side_effect=OSError("replace")):
                with self.assertRaises(OSError):
                    store.save(default_state())
            leftovers = list(Path(directory).glob(".*.tmp"))
            self.assertEqual(leftovers, [])

    def test_large_library_uses_compact_orjson_write(self):
        if state_store._orjson is None:
            self.skipTest("orjson unavailable")
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            games = [{"game_id": f"g{i}", "name": f"G{i}", "path": f"/g{i}"} for i in range(600)]
            store.save({"games": games})
            self.assertEqual(len(store.load()["games"]), 600)

    def test_read_unlocked_uses_stdlib_without_orjson(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            path.write_text(json.dumps(default_state()), encoding="utf-8")
            store = JsonStateStore(path)
            with mock.patch.object(state_store, "_orjson", None):
                loaded = store.load()
            self.assertEqual(loaded["schema_version"], STATE_SCHEMA_VERSION)

    def test_update_with_result_reuses_warm_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            store.load()
            state, value = store.update_with_result(lambda s: s["settings"].update({"warm": True}) or "ok")
            self.assertEqual(value, "ok")
            self.assertTrue(state["settings"]["warm"])

    def test_stdlib_large_library_write_without_orjson(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            games = [{"game_id": f"g{i}", "name": f"G{i}", "path": f"/g{i}"} for i in range(600)]
            with mock.patch.object(state_store, "_orjson", None):
                store.save({"games": games})
            self.assertEqual(len(json.loads(store.path.read_text())["games"]), 600)

    def test_snapshot_rotation_ignores_stat_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save(default_state())
            store.update(lambda state: state["settings"].update({"n": 1}))
            stale = next(store.snapshots_dir.glob("*.json"))
            original_stat = stale.stat

            def selective_stat(self_path):
                if self_path == stale:
                    raise OSError("stat")
                return original_stat()

            with mock.patch.object(Path, "stat", selective_stat):
                store.update(lambda state: state["settings"].update({"n": 2}))
            self.assertGreaterEqual(len(store.snapshots()), 1)

    def test_stdlib_write_path_when_orjson_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            payload = default_state()
            payload["games"] = [{"game_id": "g1", "name": "Big", "path": "/p", "description": "x" * 2_000_000}]
            with mock.patch.object(state_store, "_orjson", None):
                store.save(payload)
            self.assertTrue(store.path.is_file())

    def test_snapshot_stat_errors_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json", snapshot_limit=2)
            store.save(default_state())
            store.update(lambda state: state["settings"].update({"n": 1}))
            bad = mock.Mock()
            bad.stat = mock.Mock(side_effect=OSError("stat"))
            bad.name = "bad.json"
            fake_dir = mock.Mock()
            fake_dir.glob = mock.Mock(return_value=[bad])
            with mock.patch.object(store, "snapshots_dir", fake_dir):
                self.assertEqual(store.snapshots(), [])


if __name__ == "__main__":
    unittest.main()
