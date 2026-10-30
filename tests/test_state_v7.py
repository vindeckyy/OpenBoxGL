"""Library format v7: envelope, v6 migration, sessions.json, and future-version refusal."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures.state.corpus import HUNDRED, make_v6_library  # noqa: E402
from pkg.state import library_v7  # noqa: E402
from state_store import (  # noqa: E402
    LIBRARY_ID_RE,
    STATE_FORMAT,
    STATE_SCHEMA_VERSION,
    JsonStateStore,
    StateCorruptError,
    StateFutureVersionError,
    default_state,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "state" / "v6"


def v6_library(games=1, **extra):
    state = {
        "schema_version": 6,
        "games": [
            {"game_id": f"game-{index:05d}", "name": f"Game {index}", "path": f"/roms/game-{index}.iso", "platform": "PS1"}
            for index in range(games)
        ],
        "profiles": {},
        "history": [],
        "settings": {"theme": "dark"},
        "playlists": [],
        "queue": [],
        "notifications": [],
        "ui_state": {},
        "active_sessions": [],
        "trash": [],
        "party_decks": [],
        "emulator_settings": {},
        "achievement_overrides": {},
    }
    state.update(extra)
    return state


class EnvelopeTests(unittest.TestCase):
    def test_default_state_carries_the_v7_envelope(self):
        state = default_state()
        self.assertEqual(state["format"], STATE_FORMAT)
        self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
        self.assertRegex(state["library_id"], LIBRARY_ID_RE)
        self.assertEqual(state["path_mode"], "absolute")
        self.assertEqual(state["library_root"], "")

    def test_library_id_is_stable_across_writes_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_text(json.dumps(v6_library(2)), encoding="utf-8")
            store = JsonStateStore(primary)
            first = store.migrate_primary_to_v7()[0]["library_id"]
            store.update(lambda state: state["games"].append({"game_id": "game-added", "name": "Added"}))
            self.assertEqual(JsonStateStore(primary).load()["library_id"], first)

    def test_envelope_mirrors_path_settings_on_every_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "library.json")
            store.save(default_state())
            root = str(Path(directory).resolve())
            state = store.update(lambda current: current["settings"].update(path_mode="portable", library_root=root))
            self.assertEqual(state["path_mode"], "portable")
            self.assertEqual(state["library_root"], root)
            on_disk = json.loads((Path(directory) / "library.json").read_text(encoding="utf-8"))
            self.assertEqual((on_disk["path_mode"], on_disk["library_root"]), ("portable", root))


class MigrationTests(unittest.TestCase):
    def test_v6_migrates_keeping_unknown_fields_and_game_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            raw = v6_library(3, unknown_top={"keep": [1, 2]})
            raw["games"][0]["unknown_game_field"] = "kept"
            primary.write_text(json.dumps(raw), encoding="utf-8")
            state, report = JsonStateStore(primary).migrate_primary_to_v7()
            on_disk = json.loads(primary.read_text(encoding="utf-8"))
            self.assertEqual((report["from_schema"], report["to_schema"]), (6, 7))
            self.assertEqual(report["counts"]["games"], 3)
            self.assertEqual(on_disk["schema_version"], 7)
            self.assertEqual(on_disk["format"], STATE_FORMAT)
            self.assertEqual(on_disk["unknown_top"], {"keep": [1, 2]})
            self.assertEqual(on_disk["games"][0]["unknown_game_field"], "kept")
            self.assertEqual([game["path"] for game in on_disk["games"]], [f"/roms/game-{i}.iso" for i in range(3)])
            self.assertNotIn("active_sessions", on_disk)
            self.assertEqual(state["library_id"], on_disk["library_id"])

    def test_v6_backup_is_byte_identical_to_the_original_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            original = (FIXTURES / "one.json").read_bytes()
            primary.write_bytes(original)
            JsonStateStore(primary).migrate_primary_to_v7()
            self.assertEqual((Path(directory) / "library.v6.backup.json").read_bytes(), original)
            self.assertFalse((Path(directory) / "migration-in-progress.json").exists())

    def test_active_sessions_move_to_sessions_json_and_come_back_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            sessions = [{"game_id": "game-00000", "session_id": "session-1", "started_at": "2026-10-28T10:00:00Z"}]
            primary.write_text(json.dumps(v6_library(1, active_sessions=sessions)), encoding="utf-8")
            report = JsonStateStore(primary).migrate_primary_to_v7()[1]
            self.assertEqual(report["counts"]["sessions_moved"], 1)
            mirrored = json.loads((Path(directory) / "sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(mirrored["sessions"], sessions)
            self.assertNotIn("active_sessions", json.loads(primary.read_text(encoding="utf-8")))
            self.assertEqual(JsonStateStore(primary).load()["active_sessions"], sessions)

    def test_clearing_sessions_removes_sessions_json(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_text(json.dumps(v6_library(1, active_sessions=[{"game_id": "game-00000"}])), encoding="utf-8")
            store = JsonStateStore(primary)
            store.migrate_primary_to_v7()
            self.assertTrue((Path(directory) / "sessions.json").exists())
            store.update(lambda state: state.__setitem__("active_sessions", []))
            self.assertFalse((Path(directory) / "sessions.json").exists())

    def test_plain_load_of_a_v6_file_migrates_it_and_writes_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_text(json.dumps(v6_library(1)), encoding="utf-8")
            state = JsonStateStore(primary).load()
            self.assertEqual(state["schema_version"], 7)
            self.assertTrue((Path(directory) / "library.v6.backup.json").is_file())
            self.assertEqual(library_v7.read_report(Path(directory))["to_schema"], 7)

    def test_migrating_a_current_library_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_text(json.dumps(v6_library(1)), encoding="utf-8")
            store = JsonStateStore(primary)
            store.migrate_primary_to_v7()
            before = primary.read_bytes()
            backup_before = (Path(directory) / "library.v6.backup.json").read_bytes()
            self.assertEqual(store.migrate_primary_to_v7(), (None, None))
            self.assertEqual(primary.read_bytes(), before)
            self.assertEqual((Path(directory) / "library.v6.backup.json").read_bytes(), backup_before)

    def test_100_and_10000_game_libraries_keep_every_game(self):
        for count in (100, 10_000):
            with self.subTest(games=count), tempfile.TemporaryDirectory() as directory:
                primary = Path(directory) / "library.json"
                original = json.dumps(v6_library(count)).encode("utf-8")
                primary.write_bytes(original)
                started = time.monotonic()
                report = JsonStateStore(primary).migrate_primary_to_v7()[1]
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual(report["counts"]["games"], count)
                self.assertEqual((Path(directory) / "library.v6.backup.json").read_bytes(), original)
                self.assertEqual(len(JsonStateStore(primary).load()["games"]), count)

    def test_committed_hundred_fixture_is_the_corpus_and_migrates(self):
        self.assertEqual(json.loads(HUNDRED.read_text(encoding="utf-8")), make_v6_library(100))
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_bytes(HUNDRED.read_bytes())
            migrated = JsonStateStore(primary).load()
            self.assertEqual(migrated["schema_version"], STATE_SCHEMA_VERSION)
            self.assertEqual(len(migrated["games"]), 100)
            self.assertEqual(migrated["games"][7]["path"], "/roms/pc/game-7.iso")

    def test_fixture_with_unknown_fields_migrates_and_keeps_vendor_data(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_bytes((FIXTURES / "one.json").read_bytes())
            JsonStateStore(primary).migrate_primary_to_v7()
            on_disk = json.loads(primary.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["games"][0]["vendor_field"], "kept across migration")
            self.assertEqual(on_disk["unknown_top_level"], {"preserved": True})


class FutureAndCorruptTests(unittest.TestCase):
    def test_future_schema_is_refused_without_rewriting_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            future = json.dumps({**v6_library(1), "schema_version": 8}).encode("utf-8")
            primary.write_bytes(future)
            with self.assertRaises(StateFutureVersionError) as raised:
                JsonStateStore(primary).load()
            self.assertEqual(raised.exception.code, "STATE_FUTURE_VERSION")
            self.assertIsInstance(raised.exception, StateCorruptError)
            self.assertEqual(primary.read_bytes(), future)
            self.assertFalse((Path(directory) / "library.v6.backup.json").exists())

    def test_truncated_and_malformed_inputs_fail_before_any_file_changes(self):
        for name in ("truncated.json", "malformed.json"):
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as directory:
                primary = Path(directory) / "library.json"
                original = (FIXTURES / name).read_bytes()
                primary.write_bytes(original)
                with self.assertRaises(StateCorruptError):
                    JsonStateStore(primary).migrate_primary_to_v7()
                self.assertEqual(primary.read_bytes(), original)
                self.assertFalse((Path(directory) / "library.v6.backup.json").exists())
                self.assertFalse((Path(directory) / "migration-in-progress.json").exists())


if __name__ == "__main__":
    unittest.main()
