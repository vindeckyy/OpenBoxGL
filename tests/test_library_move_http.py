"""Library storage handlers and the in-process CLI branches (ADR 0090, plan W1-05 and W3-05).

Handlers are called directly with a recording stand-in for the HTTP handler. Every
test runs against a temporary data directory, never the developer's library.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_errors import BadRequest, MovePlanStale  # noqa: E402
from handlers import library_storage  # noqa: E402
from pkg.state import library_cli, library_v7  # noqa: E402
from state_store import JsonStateStore  # noqa: E402


class _Recorder:
    def __init__(self):
        self.status = None
        self.payload = None

    def send_json(self, status, payload, extra_headers=None):
        self.status, self.payload = status, payload


def _v6(games=1):
    return {
        "schema_version": 6,
        "games": [{"game_id": f"game-{i}", "name": f"G{i}", "path": f"/roms/g{i}.iso"} for i in range(games)],
        "profiles": {}, "history": [], "settings": {}, "playlists": [], "queue": [], "notifications": [],
        "ui_state": {}, "active_sessions": [], "trash": [], "party_decks": [], "emulator_settings": {},
        "achievement_overrides": {},
    }


class StorageHandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.new_root = self.base / "games"
        self.new_root.mkdir()
        self.patch = mock.patch.object(library_storage, "DATA", self.base / "library.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._tmp.cleanup()

    def test_integrity_serves_the_read_only_report(self):
        handler = _Recorder()
        library_storage.integrity(handler, None)
        self.assertEqual(handler.status, 200)
        self.assertIn("status", handler.payload)
        self.assertIn("exit_code", handler.payload)

    def test_move_preview_requires_an_existing_new_folder(self):
        with self.assertRaises(BadRequest):
            library_storage.move_preview(_Recorder(), {"old_root": "/old", "new_root": str(self.base / "missing")})
        with self.assertRaises(BadRequest):
            library_storage.move_preview(_Recorder(), ["not", "a", "dict"])

    def test_move_preview_returns_a_plan_with_an_id(self):
        handler = _Recorder()
        library_storage.move_preview(handler, {"old_root": str(self.base / "old"), "new_root": str(self.new_root)})
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.payload["plan_id"].startswith("mv-"))
        self.assertEqual(handler.payload["counts"]["absolute_inside_old"], 0)

    def test_move_apply_with_a_stale_plan_is_refused(self):
        store = JsonStateStore(self.base / "library.json")
        store.save(_v6(1))
        with self.assertRaises(MovePlanStale):
            library_storage.move_apply(_Recorder(), {
                "old_root": str(self.base / "old"), "new_root": str(self.new_root), "plan_id": "mv-stale",
            })
        self.assertNotEqual(json.loads((self.base / "library.json").read_text(encoding="utf-8")).get("path_mode"), "portable")

    def test_move_apply_switches_to_portable_when_the_plan_matches(self):
        old_root = self.base / "old"
        old_root.mkdir()
        (old_root / "roms").mkdir()
        (self.new_root / "roms").mkdir()
        (self.new_root / "roms" / "g0.iso").write_text("x", encoding="utf-8")
        store = JsonStateStore(self.base / "library.json")
        raw = _v6(1)
        raw["games"][0]["path"] = str(old_root / "roms" / "g0.iso")
        store.save(raw)
        plan = library_v7.plan_move(store.load()["games"], str(old_root), str(self.new_root))
        handler = _Recorder()
        with mock.patch.object(library_storage, "update_state_with_result", store.update_with_result):
            library_storage.move_apply(handler, {
                "old_root": str(old_root), "new_root": str(self.new_root), "plan_id": plan["plan_id"],
            })
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.payload["moved"], 1)
        self.assertEqual(json.loads((self.base / "library.json").read_text(encoding="utf-8"))["games"][0]["path"],
                         "@library/roms/g0.iso")


class CliBranchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.primary = self.directory / "library.json"
        self.primary.write_text(json.dumps(_v6(1)), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *argv, input_fn=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = library_cli.main(list(argv), data_dir=self.directory, input_fn=input_fn or (lambda _prompt: ""))
        return code, out.getvalue(), err.getvalue()

    def test_usage_when_no_command_or_two_commands(self):
        self.assertEqual(self._run()[0], library_cli.EXIT_USAGE)
        self.assertEqual(self._run("--check-library", "--repair-library")[0], library_cli.EXIT_USAGE)

    def test_check_and_repair_report_json(self):
        code, out, _err = self._run("--check-library")
        self.assertEqual(code, library_cli.EXIT_WARNINGS)
        self.assertIn('"schema_outdated"', out)
        self.assertEqual(self._run("--repair-library")[0], library_cli.EXIT_REFUSED)
        self._run("--migrate-library")
        code, out, _err = self._run("--repair-library")
        self.assertEqual(code, library_cli.EXIT_OK)
        self.assertIn('"status"', out)

    def test_migrate_refuses_while_a_server_answers(self):
        with mock.patch.object(library_v7, "server_running", return_value=True):
            code, _out, err = self._run("--migrate-library")
        self.assertEqual(code, library_cli.EXIT_REFUSED)
        self.assertIn("OpenBox is running", err)

    def test_migrate_then_rollback_with_yes(self):
        original = self.primary.read_bytes()
        code, out, _err = self._run("--migrate-library")
        self.assertEqual(code, library_cli.EXIT_OK)
        self.assertIn('"migrated": true', out)
        code, _out, _err = self._run("--rollback-library", "--yes")
        self.assertEqual(code, library_cli.EXIT_OK)
        self.assertEqual(self.primary.read_bytes(), original)

    def test_interactive_rollback_needs_the_typed_word(self):
        self._run("--migrate-library")
        migrated = self.primary.read_bytes()
        fake_tty = mock.Mock()
        fake_tty.isatty.return_value = True
        with mock.patch("sys.stdin", fake_tty):
            code, _out, err = self._run("--rollback-library", input_fn=lambda _prompt: "no")
        self.assertEqual(code, library_cli.EXIT_REFUSED)
        self.assertIn("cancelled", err)
        self.assertEqual(self.primary.read_bytes(), migrated)
        with mock.patch("sys.stdin", fake_tty):
            code, _out, _err = self._run("--rollback-library", input_fn=lambda _prompt: "ROLLBACK")
        self.assertEqual(code, library_cli.EXIT_OK)

    def test_rollback_without_yes_and_without_a_tty_refuses(self):
        self._run("--migrate-library")
        code, _out, err = self._run("--rollback-library")
        self.assertEqual(code, library_cli.EXIT_REFUSED)
        self.assertIn("--yes", err)


if __name__ == "__main__":
    unittest.main()
