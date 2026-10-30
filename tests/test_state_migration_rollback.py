"""Library v7 migration faults, rollback safety, check exit codes, and repair."""

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pkg.state import library_v7  # noqa: E402
from state_store import JsonStateStore, StateBackupError, StateMigrationError, default_state  # noqa: E402


def v6_library(games=2, **extra):
    state = {
        "schema_version": 6,
        "games": [{"game_id": f"game-{i:05d}", "name": f"Game {i}", "platform": "PS1"} for i in range(games)],
        "profiles": {}, "history": [], "settings": {}, "playlists": [], "queue": [], "notifications": [],
        "ui_state": {}, "active_sessions": [], "trash": [], "party_decks": [], "emulator_settings": {},
        "achievement_overrides": {},
    }
    state.update(extra)
    return state


class MigrationFaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.primary = self.directory / "library.json"
        self.original = json.dumps(v6_library(2)).encode("utf-8")
        self.primary.write_bytes(self.original)

    def tearDown(self):
        self._tmp.cleanup()

    def test_crash_after_backup_before_primary_write_is_retried_on_next_start(self):
        def crash(self, *args, **kwargs):
            raise OSError("simulated power loss before the primary is replaced")

        with mock.patch.object(JsonStateStore, "_write_unlocked", crash):
            with self.assertRaises(StateMigrationError):
                JsonStateStore(self.primary).migrate_primary_to_v7()
        self.assertEqual(self.primary.read_bytes(), self.original)
        self.assertEqual((self.directory / "library.v6.backup.json").read_bytes(), self.original)
        self.assertTrue((self.directory / "migration-in-progress.json").is_file())

        state, report = JsonStateStore(self.primary).migrate_primary_to_v7()
        self.assertEqual(state["schema_version"], 7)
        self.assertEqual(report["counts"]["games"], 2)
        self.assertFalse((self.directory / "migration-in-progress.json").exists())

    def test_failed_v6_copy_is_a_backup_error_and_the_primary_is_untouched(self):
        real_write = library_v7.atomic_write_bytes

        def fail_for_backup(path, data, **kwargs):
            if Path(path).name == "library.v6.backup.json":
                raise OSError(28, "No space left on device")
            return real_write(path, data, **kwargs)

        with mock.patch.object(library_v7, "atomic_write_bytes", fail_for_backup):
            with self.assertRaises(StateBackupError) as raised:
                JsonStateStore(self.primary).migrate_primary_to_v7()
        self.assertEqual(raised.exception.code, "STATE_BACKUP_MISSING")
        self.assertEqual(self.primary.read_bytes(), self.original)
        self.assertFalse((self.directory / "library.v6.backup.json").exists())

    def test_disk_full_while_replacing_the_primary_keeps_the_original_and_leaves_no_temp_files(self):
        def disk_full(self, *args, **kwargs):
            raise OSError(28, "No space left on device")

        with mock.patch.object(JsonStateStore, "_write_unlocked", disk_full):
            with self.assertRaises(StateMigrationError):
                JsonStateStore(self.primary).migrate_primary_to_v7()
        self.assertEqual(self.primary.read_bytes(), self.original)
        self.assertEqual([p for p in self.directory.iterdir() if p.name.endswith(".tmp")], [])

    def test_report_failure_after_the_primary_is_written_does_not_fail_startup(self):
        real_write_json = library_v7._write_json

        def fail_report(path, payload):
            if Path(path).name == "migration-report.json":
                raise OSError(5, "I/O error")
            return real_write_json(path, payload)

        with mock.patch.object(library_v7, "_write_json", fail_report):
            state, report = JsonStateStore(self.primary).migrate_primary_to_v7()
        self.assertEqual(state["schema_version"], 7)
        self.assertIsNotNone(report)
        self.assertIsNone(library_v7.read_report(self.directory))
        self.assertTrue((self.directory / "migration-in-progress.json").is_file())

        state, report = JsonStateStore(self.primary).migrate_primary_to_v7()
        self.assertIsNone(state)
        self.assertTrue(report["reconstructed"])
        self.assertEqual(library_v7.read_report(self.directory)["counts"]["games"], 2)
        self.assertFalse((self.directory / "migration-in-progress.json").exists())


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.primary = self.directory / "library.json"
        self.original = json.dumps(v6_library(2)).encode("utf-8")
        self.primary.write_bytes(self.original)
        self.store = JsonStateStore(self.primary)
        self.store.migrate_primary_to_v7()

    def tearDown(self):
        self._tmp.cleanup()

    def test_preview_changes_nothing(self):
        before = self.primary.read_bytes()
        result = library_v7.rollback(self.directory, confirmed=False)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.primary.read_bytes(), before)

    def test_rollback_restores_exact_v6_bytes_and_reports_discarded_edits(self):
        self.store.update(lambda state: state["games"].append({"game_id": "game-added", "name": "Added later"}))
        v7_bytes = self.primary.read_bytes()
        preview = library_v7.rollback(self.directory, confirmed=False)
        self.assertEqual(preview["lost"]["games_added"], 1)
        result = library_v7.rollback(self.directory, confirmed=True)
        self.assertEqual(self.primary.read_bytes(), self.original)
        self.assertEqual((self.directory / "library.json.bak").read_bytes(), self.original)
        self.assertEqual((self.directory / result["kept"]).read_bytes(), v7_bytes)
        self.assertEqual(result["lost"]["examples"]["games_added"], ["game-added"])
        self.assertFalse((self.directory / "migration-report.json").exists())
        restored = json.loads((self.directory / "rollback-report.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["restored"], "library.v6.backup.json")

    def test_rollback_refuses_while_a_server_answers_on_the_recorded_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        (self.directory / "server.port").write_text(str(listener.getsockname()[1]), encoding="utf-8")
        before = self.primary.read_bytes()
        with self.assertRaises(library_v7.RollbackRefused):
            library_v7.rollback(self.directory, confirmed=True)
        self.assertEqual(self.primary.read_bytes(), before)

    def test_rollback_refuses_while_sessions_are_recorded(self):
        (self.directory / "sessions.json").write_text(
            json.dumps({"format": 1, "sessions": [{"game_id": "game-00000"}]}), encoding="utf-8"
        )
        before = self.primary.read_bytes()
        with self.assertRaises(library_v7.RollbackRefused):
            library_v7.rollback(self.directory, confirmed=True)
        self.assertEqual(self.primary.read_bytes(), before)

    def test_rollback_refuses_without_a_v6_copy(self):
        (self.directory / "library.v6.backup.json").unlink()
        with self.assertRaises(library_v7.RollbackRefused):
            library_v7.rollback(self.directory, confirmed=True)


class CheckAndRepairTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.primary = self.directory / "library.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write_raw(self, payload):
        # Written raw on purpose: the store normalizes duplicate ids and trash on every commit.
        self.primary.write_text(json.dumps(payload), encoding="utf-8")

    def test_check_exit_codes_follow_the_severity_of_findings(self):
        self.assertEqual(library_v7.check_library(self.directory)["exit_code"], 0)
        JsonStateStore(self.primary).save(default_state())
        self.assertEqual(library_v7.check_library(self.directory)["status"], "healthy")
        state = default_state()
        state["games"] = [{"game_id": "game-1", "name": "Missing file", "path": "/nonexistent/x.iso"}]
        self.write_raw(state)
        report = library_v7.check_library(self.directory)
        self.assertEqual((report["status"], report["exit_code"]), ("warnings", 1))
        self.assertEqual(report["categories"]["game_path_missing"]["count"], 1)
        state["games"].append({"game_id": "game-1", "name": "Duplicate"})
        self.write_raw(state)
        report = library_v7.check_library(self.directory)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["categories"]["game_duplicate_id"]["count"], 1)

    def test_check_reports_a_future_schema_as_an_error_without_changing_it(self):
        future = json.dumps({**v6_library(1), "schema_version": 8}).encode("utf-8")
        self.primary.write_bytes(future)
        report = library_v7.check_library(self.directory)
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("schema_future", report["categories"])
        self.assertEqual(self.primary.read_bytes(), future)

    def test_repair_prunes_expired_trash_and_never_removes_games(self):
        state = default_state()
        state["games"] = [{"game_id": "game-a", "name": "A"}, {"game_id": "game-b", "name": "B"}]
        state["trash"] = [
            {"game_id": "game-old", "trashed_at": "2000-01-01T00:00:00Z"},
            {"game_id": "game-new", "trashed_at": "2999-01-01T00:00:00Z"},
        ]
        self.write_raw(state)
        result = library_v7.repair_library(self.directory)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(result["trash_entries_removed"], 1)
        reloaded = JsonStateStore(self.primary).load()
        self.assertEqual([game["game_id"] for game in reloaded["games"]], ["game-a", "game-b"])
        self.assertEqual([entry["game_id"] for entry in reloaded["trash"]], ["game-new"])

    def test_repair_refuses_a_library_that_still_needs_migration(self):
        self.write_raw(v6_library(1))
        with self.assertRaises(library_v7.RollbackRefused):
            library_v7.repair_library(self.directory)
        self.assertFalse((self.directory / "library.v6.backup.json").exists())


if __name__ == "__main__":
    unittest.main()
