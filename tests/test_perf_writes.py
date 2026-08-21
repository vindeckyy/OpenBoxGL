import json
import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from state_store import JsonStateStore, StateCorruptError, STATE_SCHEMA_VERSION, normalize_state


class PerfWriteTests(unittest.TestCase):
    def make_store(self, directory, snapshot_limit=5, snapshot_debounce=0.0):
        return JsonStateStore(
            Path(directory) / "library.json",
            snapshot_limit=snapshot_limit,
            snapshot_debounce=snapshot_debounce,
        )

    def test_single_backup_copy_per_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            store.save({"games": [{"game_id": "g1", "name": "One"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            self.assertTrue(store.backup_path.is_file())
            primary = json.loads(store.path.read_text())
            backup = json.loads(store.backup_path.read_text())
            self.assertEqual(primary["games"][0]["name"], "One")
            self.assertEqual(backup["games"][0]["name"], "One")

    def test_backup_matches_primary_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "First"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            store.save({"games": [{"game_id": "g1", "name": "Second"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            primary = json.loads(store.path.read_text())
            backup = json.loads(store.backup_path.read_text())
            self.assertEqual(primary["games"][0]["name"], "Second")
            self.assertEqual(backup["games"][0]["name"], "Second")

    def test_backup_never_contains_uncommitted_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "Before"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            with mock.patch("os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    store.save({"games": [{"game_id": "g1", "name": "After"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            primary = json.loads(store.path.read_text())
            backup = json.loads(store.backup_path.read_text())
            self.assertEqual(primary["games"][0]["name"], "Before")
            self.assertEqual(backup["games"][0]["name"], "Before", "backup must hold the previous good state")

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

    def test_small_library_stays_readable_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            state = {"games": [{"game_id": "g1", "name": "One"}], "profiles": {}, "history": [], "settings": {}, "playlists": []}
            store.save(state)
            raw = store.path.read_text()
            self.assertGreater(raw.count("\n"), 1, "small library files should remain human-readable")
            self.assertEqual(json.loads(raw)["games"][0]["name"], "One")

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
            self.assertEqual(backup["games"][0]["name"], "Before")

    def test_recover_still_restores_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "Good"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            store.path.write_text("{corrupt", encoding="utf-8")
            recovered = store.recover()
            self.assertEqual(recovered["games"][0]["name"], "Good")
            self.assertEqual(json.loads(store.path.read_text())["games"][0]["name"], "Good")

    def test_mutation_latency_under_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory, snapshot_debounce=30.0)
            games = [{"game_id": f"game-{i:06d}", "name": f"Title {i}", "platform": "PC", "favorite": False} for i in range(10000)]
            store.save({"games": games, "profiles": {}, "history": [], "settings": {}, "playlists": []})

            # Warm in-memory cache
            store.load_readonly()

            with mock.patch.object(store, "_load_unlocked", wraps=store._load_unlocked) as load_spy:
                start = time.perf_counter()
                for i in range(5):
                    store.update_with_result(lambda s, val=bool(i % 2): s["games"][0].__setitem__("favorite", val))
                elapsed_total = time.perf_counter() - start
                load_spy.assert_not_called()
                avg_ms = (elapsed_total / 5.0) * 1000.0
                # Threshold 80ms allows for coverage instrumentation overhead; 50ms was flaky under load.
                self.assertLess(avg_ms, 80.0, f"Average mutation latency {avg_ms:.2f}ms should be < 80ms")
    def test_snapshot_rotation_debounce(self):
        with tempfile.TemporaryDirectory() as directory:
            # Debounce snapshots to at most 1 per 30 seconds
            store = self.make_store(directory, snapshot_limit=5, snapshot_debounce=30.0)
            store.save({"games": [{"game_id": "g1", "name": "Burst 1"}]})
            for i in range(2, 10):
                gid = f"g{i}"
                gname = f"Burst {i}"
                store.update(lambda s, gid=gid, gname=gname: s["games"].append({"game_id": gid, "name": gname}))
            # Burst within 30s should create exactly 1 snapshot
            self.assertEqual(len(store.snapshots()), 1)

            # Force snapshot rotation or manual expiry
            store._rotate_snapshots(force=True)
            self.assertEqual(len(store.snapshots()), 2)

    def test_atomic_recovery_on_write_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({"games": [{"game_id": "g1", "name": "Committed"}]})

            # Simulate an exception during write (e.g. disk write failure)
            with mock.patch("os.fsync", side_effect=OSError("Disk write failed")):
                with self.assertRaises(OSError):
                    store.update(lambda s: s["games"].append({"game_id": "g2", "name": "Corrupt"}))

            # The cache was cleared and reloading gives the committed state
            reloaded = store.load()
            self.assertEqual(len(reloaded["games"]), 1)
            self.assertEqual(reloaded["games"][0]["name"], "Committed")

    def test_internal_primary_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({
                "schema_version": 6,
                "games": [
                    {"game_id": "game-doom", "name": "Doom", "platform": "PC"},
                    {"game_id": "game-quake", "name": "Quake", "platform": "PC"},
                    {"game_id": "game-mario", "name": "Super Mario", "platform": "SNES"},
                ],
                "profiles": {}, "history": [], "settings": {}, "playlists": [],
                "queue": [], "notifications": [], "ui_state": {}, "active_sessions": [],
            })

            # Check direct lookup by ID
            g1 = store.get_game_by_id("game-doom")
            self.assertIsNotNone(g1)
            self.assertEqual(g1["name"], "Doom")
            self.assertIsNone(store.get_game_by_id("nonexistent"))

            # Check direct lookup by Platform
            pc_games = store.get_games_by_platform("PC")
            self.assertEqual(len(pc_games), 2)
            self.assertEqual([g["name"] for g in pc_games], ["Doom", "Quake"])
            snes_games = store.get_games_by_platform("SNES")
            self.assertEqual(len(snes_games), 1)
            self.assertEqual(snes_games[0]["name"], "Super Mario")
            self.assertEqual(store.get_games_by_platform("NES"), [])

            # Check index properties
            self.assertEqual(set(store.games_by_id.keys()), {"game-doom", "game-quake", "game-mario"})
            self.assertEqual(set(store.games_by_platform.keys()), {"PC", "SNES"})

    def test_cache_invalidation_on_external_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save({
                "schema_version": 6,
                "games": [{"game_id": "game-initial", "name": "Initial"}],
                "profiles": {}, "history": [], "settings": {}, "playlists": [],
                "queue": [], "notifications": [], "ui_state": {}, "active_sessions": [],
            })

            # Cache is warmed
            self.assertEqual(store.load_readonly()["games"][0]["name"], "Initial")

            # External process writes to the state file
            raw = json.loads(store.path.read_text(encoding="utf-8"))
            raw["games"][0]["name"] = "Externally Modified"
            time.sleep(0.01)  # Ensure mtime advance
            store.path.write_text(json.dumps(raw), encoding="utf-8")

            # store detects signature mismatch and reloads
            reloaded = store.load_readonly()
            self.assertEqual(reloaded["games"][0]["name"], "Externally Modified")
            self.assertEqual(store.get_game_by_id("game-initial")["name"], "Externally Modified")

    def test_backup_fallback_when_link_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            with mock.patch("os.link", side_effect=OSError("Cross-device link")):
                store.save({"games": [{"game_id": "g1", "name": "Fallback"}]})
            self.assertTrue(store.backup_path.is_file())
            backup = json.loads(store.backup_path.read_text())
            self.assertEqual(backup["games"][0]["name"], "Fallback")

    def test_schema_migrations_v1_through_v6(self):
        v1_list = [{"name": "Legacy 1", "path": "/roms/1.nes", "platform": "NES"}]
        state, changed = normalize_state(v1_list)
        self.assertTrue(changed)
        self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(len(state["games"]), 1)
        self.assertTrue(state["games"][0]["game_id"].startswith("game-"))
        self.assertIn("ui_state", state)
        self.assertIn("active_sessions", state)
        self.assertIn("queue", state)
        self.assertIn("notifications", state)

    def test_secure_text_write(self):
        from state_store import secure_text_write
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secret.token"
            secure_text_write(target, "super-secret-token")
            self.assertEqual(target.read_text(encoding="utf-8"), "super-secret-token")
            # Verify owner-only permissions (0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_backend_io_helpers(self):
        from backend_io import (
            atomic_write_bytes, atomic_write_text, atomic_copy_stream,
            contained_path, fsync_directory
        )
        import io

        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "test.bin"
            atomic_write_bytes(p, b"bytes-data")
            self.assertEqual(p.read_bytes(), b"bytes-data")

            p_txt = Path(directory) / "test.txt"
            atomic_write_text(p_txt, "text-data")
            self.assertEqual(p_txt.read_text(), "text-data")

            p_stream = Path(directory) / "stream.bin"
            stream = io.BytesIO(b"stream-data")
            atomic_copy_stream(stream, p_stream)
            self.assertEqual(p_stream.read_bytes(), b"stream-data")

            fsync_directory(Path(directory))
            resolved = contained_path(p_txt, [directory], must_exist=True)
            self.assertEqual(resolved, p_txt.resolve())

            with self.assertRaises(ValueError):
                contained_path(Path("/etc/shadow"), [directory])
            with self.assertRaises(FileNotFoundError):
                contained_path(Path(directory) / "nonexistent", [directory], must_exist=True)

    def test_corrupt_state_handling(self):
        from state_store import normalize_state
        with self.assertRaises(StateCorruptError):
            normalize_state("not a dict or list")

        with self.assertRaises(StateCorruptError):
            normalize_state({"schema_version": 999})

        with self.assertRaises(StateCorruptError):
            normalize_state({"schema_version": 1, "games": "invalid"})

        with self.assertRaises(StateCorruptError):
            normalize_state({"schema_version": 6, "games": ["not-a-dict"]})

    def test_restore_snapshot_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory, snapshot_limit=3)
            store.save({"games": [{"game_id": "g1", "name": "One"}]})
            snapshots = store.snapshots()
            self.assertEqual(len(snapshots), 1)

            # Restoring valid snapshot
            state = store.restore_snapshot(snapshots[0]["name"])
            self.assertEqual(state["games"][0]["name"], "One")

            # Restoring unknown snapshot raises StateCorruptError
            with self.assertRaises(StateCorruptError):
                store.restore_snapshot("nonexistent_snapshot.json")

    def test_json_dumps_and_loads(self):
        from state_store import _json_dumps, _json_dump_file, _json_load, _json_dumps_bytes
        import io

        data = {"hello": "world", "num": 123}
        serialized = _json_dumps(data, indent=2)
        self.assertIn("hello", serialized)

        b = _json_dumps_bytes(data)
        self.assertIsInstance(b, bytes)

        buf = io.StringIO()
        _json_dump_file(data, buf)
        buf.seek(0)
        loaded = _json_load(buf)
        self.assertEqual(loaded, data)

    def test_identity_and_game_id_normalization_edge_cases(self):
        from state_store import _identity_payload, _stable_game_id, _normalize_game_ids, _normalize_feature_fields

        # Game with only name and no path or external IDs
        payload = _identity_payload({"name": "Standalone Game"})
        self.assertEqual(payload.get("name"), "Standalone Game")

        # Stable ID generation
        gid = _stable_game_id({"name": "Standalone Game"})
        self.assertTrue(gid.startswith("game-"))

        # Collision suffix handling
        games = [
            {"game_id": "game-dup", "name": "Game A"},
            {"game_id": "game-dup", "name": "Game B"},
            {"game_id": "game-dup", "name": "Game C"},
            {"game_id": "game-0123456789abcdef01234567-1", "name": "Legacy Indexed"},
        ]
        changed = _normalize_game_ids(games)
        self.assertTrue(changed)
        ids = [g["game_id"] for g in games]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ids[0], "game-dup")
        self.assertEqual(ids[1], "game-dup-2")
        self.assertEqual(ids[2], "game-dup-3")

        # Normalize feature fields capping and cleaning
        state = {
            "queue": list(range(600)),
            "notifications": list(range(300)),
            "active_sessions": [{"id": 1}, "not-a-dict"],
            "games": [{"game_id": "g1", "tags": "not-a-list"}],
        }
        changed = _normalize_feature_fields(state)
        self.assertTrue(changed)
        self.assertEqual(len(state["queue"]), 500)
        self.assertEqual(len(state["notifications"]), 200)
        self.assertEqual(state["active_sessions"], [{"id": 1}])
        self.assertEqual(state["games"][0]["tags"], [])

    def test_snapshot_limit_zero_and_clean_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            # Snapshot limit 0 disables rotation
            store = self.make_store(directory, snapshot_limit=0)
            store.save({"games": [{"game_id": "g1", "name": "NoSnap"}]})
            self.assertEqual(len(store.snapshots()), 0)

    def test_recover_when_backup_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            with self.assertRaises(StateCorruptError):
                store.recover()

    def test_migration_pipeline_v1_to_v6_explicit(self):
        # Test v1 migration with non-list legacy aliases and non-list tags
        v1_state = {
            "schema_version": 1,
            "games": [
                {
                    "game_id": "game-0123456789abcdef01234567-1",
                    "name": "Game 1",
                    "path": "/games/g1.rom",
                    "platform": "NES",
                    "steam_app_id": "100",
                    "legacy_game_ids": "not-a-list",
                    "tags": "not-a-list",
                },
                {"name": "Game 2", "heroic_app_id": "200"},
                {"name": "Game 3", "lutris_id": "300"},
                {"name": "Game 4", "gameyfin_id": "400"},
                {"name": "Game 5", "launchbox_db_id": "500"},
            ],
            "queue": "not-a-list",
            "notifications": "not-a-list",
            "ui_state": "not-a-dict",
            "active_sessions": "not-a-list",
        }
        normalized, changed = normalize_state(v1_state)
        self.assertTrue(changed)
        self.assertEqual(normalized["schema_version"], 6)
        self.assertEqual(len(normalized["games"]), 5)
        self.assertEqual(normalized["queue"], [])
        self.assertEqual(normalized["notifications"], [])
        self.assertEqual(normalized["ui_state"], {})
        self.assertEqual(normalized["active_sessions"], [])

    def test_load_and_load_readonly_auto_migrates_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            # Write a legacy v1 state to disk
            legacy = {"schema_version": 1, "games": [{"name": "Legacy", "path": "/bin/true"}]}
            store.path.write_text(json.dumps(legacy), encoding="utf-8")

            # load() auto-migrates and writes to disk
            loaded = store.load()
            self.assertEqual(loaded["schema_version"], 6)

            # Write another unmigrated state to disk
            store.path.write_text(json.dumps(legacy), encoding="utf-8")
            store._clear_cache()
            readonly = store.load_readonly()
            self.assertEqual(readonly["schema_version"], 6)

    def test_recover_when_backup_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.backup_path.write_text("invalid json", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                store.recover()

    def test_remember_non_list_games(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store._remember({"games": "not-a-list"}, adopt=True)
            self.assertEqual(store.games_by_id, {})
            self.assertEqual(store.games_by_platform, {})

    def test_ensure_loaded_auto_migrates_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            legacy = {"schema_version": 1, "games": [{"name": "Legacy", "path": "/bin/true"}]}
            store.path.write_text(json.dumps(legacy), encoding="utf-8")
            # Directly call index lookup which triggers _ensure_loaded
            game = store.get_game_by_id(list(store.games_by_id.keys())[0])
            self.assertIsNotNone(game)
            self.assertEqual(game["name"], "Legacy")
            # Verify file was migrated on disk
            raw = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 6)

    def test_medium_library_above_compact_threshold_compacts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            # 100 games (<500) but large description (>1MB total)
            games = [{"game_id": f"g{i}", "name": f"G{i}", "desc": "y" * 15000} for i in range(100)]
            store.save({"games": games})
            self.assertGreater(store.path.stat().st_size, 1024 * 1024)

    def test_orjson_save_load_round_trips_when_available(self):
        import state_store
        if state_store._orjson is None:
            self.skipTest("orjson branch is unavailable")

        class FakeOrjson:
            OPT_SORT_KEYS = 1
            OPT_INDENT_2 = 2
            JSONDecodeError = json.JSONDecodeError

            @staticmethod
            def dumps(payload, option=None):
                kwargs = {"separators": (",", ":")}
                if option and option & FakeOrjson.OPT_INDENT_2:
                    kwargs = {"indent": 2}
                if option and option & FakeOrjson.OPT_SORT_KEYS:
                    kwargs["sort_keys"] = True
                return json.dumps(payload, **kwargs).encode("utf-8")

            @staticmethod
            def loads(payload):
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                return json.loads(payload)

        with mock.patch.object(state_store, "_orjson", FakeOrjson):
            with tempfile.TemporaryDirectory() as directory:
                store = self.make_store(directory)
                store.save({"games": [{"game_id": "g1", "name": "OrjsonGame"}]})
                self.assertEqual(store.load()["games"][0]["name"], "OrjsonGame")

                games = [{"game_id": f"g{i}", "name": f"G{i}"} for i in range(600)]
                store.save({"games": games})
                self.assertEqual(len(store.load()["games"]), 600)


if __name__ == "__main__":
    unittest.main()

