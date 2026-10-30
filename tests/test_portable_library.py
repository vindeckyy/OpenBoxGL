"""Portable library round trip (ADR 0090): tokens on disk, absolute paths for consumers, a rebasing move."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_errors import MovePlanStale  # noqa: E402
from pkg.state import library_v7  # noqa: E402
from state_store import JsonStateStore, default_state  # noqa: E402


class PortableLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.old_root = self.base / "games-old"
        self.new_root = self.base / "games-new"
        (self.old_root / "roms").mkdir(parents=True)
        (self.new_root / "roms").mkdir(parents=True)
        (self.new_root / "roms" / "fixture.iso").write_text("data", encoding="utf-8")
        self.primary = self.base / "data" / "library.json"
        self.store = JsonStateStore(self.primary)
        self.store.save(default_state())

    def tearDown(self):
        self._tmp.cleanup()

    def _add_game(self, game_id, path):
        self.store.update(lambda state: state["games"].append({"game_id": game_id, "name": game_id, "path": path}))

    def _move(self, plan_id=None):
        plan = library_v7.plan_move(self.store.load()["games"], str(self.old_root), str(self.new_root))

        def mutate(state):
            return library_v7.apply_move(state, plan_id or plan["plan_id"], str(self.old_root), str(self.new_root))

        return plan, self.store.update_with_result(mutate)[1]

    def test_portable_paths_are_stored_as_tokens_and_read_back_as_absolute(self):
        self._add_game("game-a", str(self.old_root / "roms" / "fixture.iso"))
        plan, result = self._move()
        self.assertEqual(result["moved"], 1)
        on_disk = json.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["games"][0]["path"], "@library/roms/fixture.iso")
        self.assertEqual((on_disk["path_mode"], on_disk["library_root"]), ("portable", str(self.new_root)))
        loaded = JsonStateStore(self.primary).load()
        self.assertEqual(loaded["games"][0]["path"], str(self.new_root / "roms" / "fixture.iso"))

    def test_changing_the_root_resolves_the_same_token_to_the_new_folder(self):
        self._add_game("game-a", str(self.old_root / "roms" / "fixture.iso"))
        self._move()
        relocated = self.base / "drive-f"
        relocated.mkdir()
        (relocated / "roms").mkdir()
        (relocated / "roms" / "fixture.iso").write_text("data", encoding="utf-8")
        raw = json.loads(self.primary.read_text(encoding="utf-8"))
        raw["library_root"] = str(relocated)
        raw["settings"]["library_root"] = str(relocated)
        self.primary.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(JsonStateStore(self.primary).load()["games"][0]["path"], str(relocated / "roms" / "fixture.iso"))

    def test_paths_outside_the_old_root_stay_absolute_and_are_reported(self):
        self._add_game("game-a", str(self.old_root / "roms" / "fixture.iso"))
        self._add_game("game-b", "/elsewhere/other.iso")
        plan, _result = self._move()
        self.assertEqual(plan["counts"]["outside"], 1)
        on_disk = json.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["games"][1]["path"], "/elsewhere/other.iso")

    def test_a_stale_plan_is_refused_and_changes_nothing(self):
        self._add_game("game-a", str(self.old_root / "roms" / "fixture.iso"))
        before = self.primary.read_bytes()
        with self.assertRaises(MovePlanStale) as raised:
            self._move(plan_id="mv-stale-plan")
        self.assertEqual(raised.exception.code, "MOVE_PLAN_STALE")
        self.assertEqual(self.primary.read_bytes(), before)

    def test_integrity_reports_a_token_that_resolves_to_a_missing_file(self):
        self._add_game("game-a", str(self.old_root / "roms" / "fixture.iso"))
        self._move()
        (self.new_root / "roms" / "fixture.iso").unlink()
        report = library_v7.check_library(self.primary.parent)
        self.assertIn("game_path_missing", report["categories"])
        self.assertEqual(report["exit_code"], 1)

    def test_escape_tokens_are_reported_not_resolved(self):
        raw = json.loads(self.primary.read_text(encoding="utf-8"))
        raw["path_mode"] = "portable"
        raw["library_root"] = str(self.new_root)
        raw["settings"]["path_mode"] = "portable"
        raw["settings"]["library_root"] = str(self.new_root)
        raw["games"] = [{"game_id": "game-x", "name": "Escape", "path": "@library/../../etc/passwd"}]
        self.primary.write_text(json.dumps(raw), encoding="utf-8")
        report = library_v7.check_library(self.primary.parent)
        self.assertEqual(report["categories"]["path_token_escape"]["count"], 1)
        self.assertEqual(report["exit_code"], 2)


class ExtraAppPathTests(unittest.TestCase):
    def test_extra_app_paths_under_the_root_become_tokens_and_expand_back(self):
        with tempfile.TemporaryDirectory() as root:
            games = [{
                "game_id": "g1",
                "path": f"{root}/roms/a.iso",
                "applications": [{"name": "Setup", "path": f"{root}/tools/setup.exe"}],
                "versions": [{"name": "v2", "path": "/elsewhere/v2.iso"}],
            }]
            encoded = library_v7.encode_games(games, root)
            self.assertTrue(encoded[0]["path"].startswith("@library/"))
            self.assertTrue(encoded[0]["applications"][0]["path"].startswith("@library/"))
            self.assertEqual(encoded[0]["versions"][0]["path"], "/elsewhere/v2.iso")
            expanded, problems = library_v7.expand_games(encoded, root)
            self.assertEqual(problems, [])
            self.assertEqual(expanded[0]["applications"][0]["path"], f"{root}/tools/setup.exe")
            self.assertEqual(games[0]["applications"][0]["path"], f"{root}/tools/setup.exe")

    def test_a_move_rebases_extra_app_paths_with_the_game_path(self):
        with tempfile.TemporaryDirectory() as old_dir, tempfile.TemporaryDirectory() as new_dir:
            games = [{
                "game_id": "g1",
                "path": str(Path(old_dir) / "roms" / "a.iso"),
                "applications": [{"name": "Setup", "path": str(Path(old_dir) / "tools" / "setup.exe")}],
            }]
            plan = library_v7.plan_move(games, old_dir, new_dir)
            self.assertEqual(plan["counts"]["absolute_inside_old"], 2)
            state = {"games": games, "settings": {}}
            result = library_v7.apply_move(state, plan["plan_id"], old_dir, new_dir)
            self.assertEqual(result["moved"], 2)
            self.assertEqual(state["games"][0]["applications"][0]["path"], str(Path(new_dir) / "tools" / "setup.exe"))



class PortableResolutionTests(unittest.TestCase):
    """Tokens resolve on load, so launch, archives, media, and health all see an absolute path."""

    def write_library(self, directory, games, root):
        data = Path(directory) / "data"
        data.mkdir(parents=True, exist_ok=True)
        state = default_state()
        state["path_mode"] = "portable"
        state["library_root"] = str(root)
        state["settings"] = {"path_mode": "portable", "library_root": str(root)}
        state["games"] = games
        (data / "library.json").write_text(json.dumps(state), encoding="utf-8")
        return data

    def test_a_token_resolves_on_load_and_is_stored_as_a_token_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rom = root / "roms" / "a.iso"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"x")
            data = self.write_library(root, [{"game_id": "g1", "name": "Game", "path": "@library/roms/a.iso"}], root)
            store = JsonStateStore(data / "library.json")
            loaded = store.load()
            # Every consumer reads an absolute path.
            self.assertEqual(loaded["games"][0]["path"], str(rom))
            # The file keeps the token, so the library stays portable.
            store.save(loaded)
            on_disk = json.loads((data / "library.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["games"][0]["path"], "@library/roms/a.iso")

    def test_a_missing_library_root_is_reported_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "not-mounted"
            data = self.write_library(root, [{"game_id": "g1", "name": "Game", "path": "@library/roms/a.iso"}], missing)
            report = library_v7.check_library(data)
            self.assertIn("library_root_missing", report["categories"])


if __name__ == "__main__":
    unittest.main()
