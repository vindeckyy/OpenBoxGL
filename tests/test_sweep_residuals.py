#!/usr/bin/env python3
"""Regression tests for the 1.10.1 sweep residuals (defects #11-#26).

Each class pins one inventoried defect from .agents/orchestrator_1/PROJECT.md;
the fixes land across pkg/parity, pkg/state, saves.py, cloud_sync.py,
handlers/media.py, and the static frontend.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_DATA_ROOT = tempfile.TemporaryDirectory(prefix="openbox-sweep-residuals-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_ROOT.name
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pkg" / "parity"))

import pkg.parity  # noqa: E402,F401  # registers the flat parity_* import finder
import saves  # noqa: E402
import cloud_sync  # noqa: E402
import pkg.parity.parity_import as parity_import  # noqa: E402
from api_errors import BadRequest  # noqa: E402
from pkg.parity.launch_tokens import PLACEHOLDERS, apply_tokens, find_invalid_tokens  # noqa: E402
from pkg.parity.parity_backup import diff_manifests  # noqa: E402
from pkg.parity.parity_launch_doctor import validate_preview  # noqa: E402
from pkg.parity.parity_launchbox_import import (  # noqa: E402
    StaleImportPlan,
    _digest,
    _find_target,
    apply_import_plan,
)
from pkg.parity.parity_setup_preview import _is_expired, commit_preview  # noqa: E402
from pkg.parity.parity_setup_preview import compute_summary, create_preview_record, preview_document, save_preview  # noqa: E402


class TestPreviewExpiryTimezone(unittest.TestCase):
    """#11: naive ``datetime.now()`` vs aware ``expires_at`` must not TypeError."""

    def _write_preview(self, directory, payload):
        previews = Path(directory) / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        (previews / "p1.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_aware_future_expiry_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            self._write_preview(directory, {"preview_id": "p1", "expires_at": future})
            payload = validate_preview("p1", directory)
            self.assertEqual(payload["preview_id"], "p1")

    def test_aware_past_expiry_raises_preview_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            self._write_preview(directory, {"preview_id": "p1", "expires_at": past})
            with self.assertRaises(BadRequest) as ctx:
                validate_preview("p1", directory)
            self.assertEqual(ctx.exception.code, "PREVIEW_EXPIRED")

    def test_naive_timestamps_still_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            past = (datetime.now() - timedelta(hours=1)).isoformat()
            self._write_preview(directory, {"preview_id": "p1", "expires_at": past})
            with self.assertRaises(BadRequest) as ctx:
                validate_preview("p1", directory)
            self.assertEqual(ctx.exception.code, "PREVIEW_EXPIRED")
            future = (datetime.now() + timedelta(hours=1)).isoformat()
            self._write_preview(directory, {"preview_id": "p1", "expires_at": future})
            self.assertEqual(validate_preview("p1", directory)["preview_id"], "p1")

    def test_garbage_expiry_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_preview(directory, {"preview_id": "p1", "expires_at": "not-a-date"})
            self.assertEqual(validate_preview("p1", directory)["preview_id"], "p1")

    def test_is_expired_tolerates_naive_and_garbage(self):
        self.assertFalse(_is_expired("not-a-date"))
        self.assertFalse(_is_expired(""))
        self.assertFalse(_is_expired((datetime.now() + timedelta(hours=1)).isoformat()))
        self.assertTrue(_is_expired((datetime.now() - timedelta(hours=1)).isoformat()))
        aware = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(_is_expired(aware))


class TestSaveRestoreMissingRoots(unittest.TestCase):
    """#12: restores must work when save roots vanished after the backup."""

    def test_restore_recreates_missing_save_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_dir = root / "saves"
            save_dir.mkdir()
            (save_dir / "slot1.sav").write_text("before")
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(save_dir)]}
            archive = saves.backup_saves(game, root / "backups")
            for child in save_dir.iterdir():
                child.unlink()
            save_dir.rmdir()
            restored = saves.restore_saves(game, root / "backups", archive.name)
            self.assertEqual((save_dir / "slot1.sav").read_text(), "before")
            self.assertTrue(restored.is_file())

    def test_restore_recreates_missing_save_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_file = root / "memcard.srm"
            save_file.write_text("state")
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(save_file)]}
            archive = saves.backup_saves(game, root / "backups")
            save_file.unlink()
            saves.restore_saves(game, root / "backups", archive.name)
            self.assertEqual(save_file.read_text(), "state")

    def test_restore_with_no_existing_roots_skips_safety_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_dir = root / "saves"
            save_dir.mkdir()
            (save_dir / "a.sav").write_text("x")
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(save_dir)]}
            archive = saves.backup_saves(game, root / "backups")
            (save_dir / "a.sav").unlink()
            save_dir.rmdir()
            # Pre-restore backup must not abort the restore when nothing exists.
            saves.restore_saves(game, root / "backups", archive.name)
            self.assertEqual((save_dir / "a.sav").read_text(), "x")

    def test_backup_saves_allow_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(root / "nope")]}
            self.assertIsNone(saves.backup_saves(game, root / "backups", allow_empty=True))
            with self.assertRaises(FileNotFoundError):
                saves.backup_saves(game, root / "backups")

    def test_type_mismatch_still_rejected_when_path_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_dir = root / "saves"
            save_dir.mkdir()
            (save_dir / "a.sav").write_text("x")
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(save_dir)]}
            archive = saves.backup_saves(game, root / "backups")
            (save_dir / "a.sav").unlink()
            save_dir.rmdir()
            save_dir.write_text("now a file")
            with self.assertRaises(ValueError):
                saves.restore_saves(game, root / "backups", archive.name)

    def test_backup_rejects_symlink_root_invalid_label_and_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.sav"
            real.write_text("x")
            link = root / "link.sav"
            link.symlink_to(real)
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(real)]}
            with mock.patch.object(saves, "save_roots", return_value=[link]):
                with self.assertRaises(ValueError):
                    saves.backup_saves(game, root / "backups")
            with self.assertRaises(ValueError):
                saves.backup_saves(game, root / "backups", label="bad label!")
            save_dir = root / "tree"
            save_dir.mkdir()
            (save_dir / "a.sav").write_text("x")
            (save_dir / "evil.sav").symlink_to(real)
            game["save_paths"] = [str(save_dir)]
            with self.assertRaises(ValueError):
                saves.backup_saves(game, root / "backups")


class TestSaveSyncLock(unittest.TestCase):
    """#20: backup/restore extraction and cloud sync serialize on SAVE_SYNC_LOCK."""

    def test_backup_and_restore_share_reentrant_lock(self):
        self.assertIs(saves.SAVE_SYNC_LOCK, cloud_sync.SAVE_SYNC_LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_dir = root / "saves"
            save_dir.mkdir()
            (save_dir / "a.sav").write_text("x")
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(save_dir)]}
            archive = saves.backup_saves(game, root / "backups")
            (save_dir / "a.sav").write_text("y")
            # restore_saves calls backup_saves internally -> needs a reentrant lock.
            saves.restore_saves(game, root / "backups", archive.name)
            self.assertEqual((save_dir / "a.sav").read_text(), "x")

    def test_backup_waits_for_lock_holder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_dir = root / "saves"
            save_dir.mkdir()
            (save_dir / "a.sav").write_text("x")
            game = {"name": "G", "path": "/roms/g", "save_paths": [str(save_dir)]}
            done = threading.Event()
            saves.SAVE_SYNC_LOCK.acquire()
            try:
                worker = threading.Thread(
                    target=lambda: (saves.backup_saves(game, root / "backups"), done.set()),
                    daemon=True,
                )
                worker.start()
                self.assertFalse(done.wait(0.5))
            finally:
                saves.SAVE_SYNC_LOCK.release()
            worker.join(timeout=10)
            self.assertTrue(done.is_set())

    def test_sync_statistics_serializes_against_save_io(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            state = {"games": [], "settings": {}}
            done = threading.Event()
            saves.SAVE_SYNC_LOCK.acquire()
            try:
                worker = threading.Thread(
                    target=lambda: (cloud_sync.sync_statistics(state, folder), done.set()),
                    daemon=True,
                )
                worker.start()
                self.assertFalse(done.wait(0.5))
            finally:
                saves.SAVE_SYNC_LOCK.release()
            worker.join(timeout=10)
            self.assertTrue(done.is_set())
            self.assertTrue((folder / "openbox-statistics.json").is_file())


class TestBiosDependencyDetection(unittest.TestCase):
    """#13: regular-file BIOS dependencies must report found=True."""

    def _detect(self, home, paths):
        hints = {"TestEmu": [(f"hint-{index}", Path.home() / f".tb-{index}") for index, _ in enumerate(paths)]}
        with mock.patch.dict(parity_import.BIOS_HINTS, hints):
            for index, maker in enumerate(paths):
                maker(home / f".tb-{index}")
            return parity_import.detect_dependencies("TestEmu", home=home)

    def test_regular_file_dependency_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = self._detect(home, [lambda p: p.write_text("x")])
            self.assertTrue(result["required"][0]["found"])
            self.assertEqual(result["missing"], [])

    def test_dir_nonempty_found_empty_and_missing_not(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)

            def make_full(p):
                p.mkdir(parents=True)
                (p / "bios.bin").write_text("x")

            def make_empty(p):
                p.mkdir(parents=True)

            result = self._detect(home, [make_full, make_empty, lambda p: None])
            found = [entry["found"] for entry in result["required"]]
            self.assertEqual(found, [True, False, False])
            self.assertEqual(len(result["missing"]), 2)

    def test_unreadable_dir_is_not_found_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".tb-0").mkdir(parents=True)
            hints = {"TestEmu": [("hint", Path.home() / ".tb-0")]}
            with mock.patch.dict(parity_import.BIOS_HINTS, hints), mock.patch.object(
                Path, "iterdir", side_effect=OSError("denied")
            ):
                result = parity_import.detect_dependencies("TestEmu", home=home)
            self.assertFalse(result["required"][0]["found"])


class TestBackupDiffNames(unittest.TestCase):
    """#14: manifest diff must track the ``name`` field, not ``title``."""

    def _archive(self, root, games):
        archive = root / "backup.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("manifest.json", json.dumps({"items": ["library"]}))
            package.writestr("library.json", json.dumps({"games": games, "settings": {}}))
        return archive

    def test_rename_is_reported_as_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self._archive(Path(directory), [{"game_id": "g1", "name": "Old Name"}])
            result = diff_manifests({"games": [{"game_id": "g1", "name": "New Name"}]}, archive)
            self.assertIn("g1", result["changed"])

    def test_unchanged_name_is_not_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self._archive(Path(directory), [{"game_id": "g1", "name": "Same"}])
            result = diff_manifests({"games": [{"game_id": "g1", "name": "Same"}]}, archive)
            self.assertNotIn("g1", result["changed"])


class TestLaunchHistoryEntry(unittest.TestCase):
    """#15: history rows need ``game_id`` and an int ``exit_code``; old
    ``[code, timed_out]`` rows are normalized on write."""

    def _finish(self, state, running, wait_result):
        import webapp_state

        def update_state(mutator):
            mutator(state)
            return state

        started = datetime.now()
        process = mock.MagicMock()
        lease = mock.MagicMock()
        webapp_state.RUNNING["lid-1"] = running
        try:
            with mock.patch("webapp_state.load_state", return_value=state), mock.patch(
                "webapp_state.update_state", side_effect=update_state
            ), mock.patch("webapp_state.wait_for_exit", return_value=wait_result), mock.patch(
                "webapp_state.session_event"
            ), mock.patch("webapp_state._publish_session_event"), mock.patch(
                "webapp_state.sync_cloud", side_effect=ValueError("off")
            ), mock.patch("webapp_state.run_plugins"):
                webapp_state.finish_session("lid-1", 0, started, process, lease)
        finally:
            webapp_state.RUNNING.pop("lid-1", None)

    def _state(self):
        return {
            "games": [{"game_id": "g1", "name": "Game", "path": "/bin/true"}],
            "profiles": {},
            "settings": {"track_session_history": True},
            "history": [],
            "active_sessions": [{"launch_id": "lid-1"}],
        }

    def test_history_entry_has_int_exit_code_and_game_id(self):
        from pkg.parity.parity_tracking import WaitResult

        state = self._state()
        running = {"stable_game_id": "g1", "game": "Game", "game_path": "/bin/true"}
        self._finish(state, running, WaitResult(exit_code=3, timed_out=True))
        entry = state["history"][-1]
        self.assertEqual(entry["exit_code"], 3)
        self.assertIsInstance(entry["exit_code"], int)
        self.assertEqual(entry["game_id"], "g1")
        json.dumps(state["history"])  # the stored row must be plain JSON

    def test_shipped_waitresult_rows_normalize_on_append(self):
        from pkg.parity.parity_tracking import WaitResult

        state = self._state()
        state["history"].append(
            {"game": "Game", "started": "2026-01-01T00:00:00", "seconds": 60, "exit_code": [2, False]}
        )
        running = {"stable_game_id": "g1", "game": "Game", "game_path": "/bin/true"}
        self._finish(state, running, WaitResult(exit_code=0, timed_out=False))
        old = state["history"][0]
        self.assertEqual(old["exit_code"], 2)
        self.assertIsInstance(old["exit_code"], int)
        self.assertEqual(old["game_id"], "g1")
        self.assertEqual(state["history"][-1]["exit_code"], 0)

    def test_normalize_history_skips_corrupt_rows(self):
        from pkg.state.launch import _normalize_history_entries

        history = [
            {"game": "Game", "exit_code": [7, True]},
            "corrupt-row",
            {"game": "Game", "exit_code": {"bad": 1}},
        ]
        games = [{"game_id": "g1", "name": "Game"}, "corrupt-game"]
        _normalize_history_entries(history, games)
        self.assertEqual(history[0]["exit_code"], 7)
        self.assertEqual(history[0]["game_id"], "g1")
        self.assertEqual(history[1], "corrupt-row")
        self.assertEqual(history[2]["exit_code"], 0)

    def test_ambiguous_names_do_not_backfill_game_id(self):
        from pkg.parity.parity_tracking import WaitResult

        state = self._state()
        state["games"].append({"game_id": "g2", "name": "Game", "path": "/bin/false"})
        state["history"].append(
            {"game": "Game", "started": "2026-01-01T00:00:00", "seconds": 10, "exit_code": [1, False]}
        )
        running = {"stable_game_id": "g1", "game": "Game", "game_path": "/bin/true"}
        self._finish(state, running, WaitResult(exit_code=0, timed_out=False))
        self.assertEqual(state["history"][0]["exit_code"], 1)
        self.assertNotIn("game_id", state["history"][0])


class TestSetupPreviewMediaGaps(unittest.TestCase):
    """#16: compute_summary must inspect ``cover``/``screenshots`` fields."""

    def _summary(self, games):
        service = mock.MagicMock()
        service.list_jobs.return_value = {"jobs": []}
        with mock.patch(
            "pkg.parity.parity_setup_preview.get_operation_service", return_value=service
        ):
            return compute_summary(state={"games": games}, which=lambda _x: None)

    def test_game_with_cover_file_is_not_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.png"
            cover.write_bytes(b"png")
            result = self._summary([{"name": "G", "path": "/x", "cover": str(cover)}])
            self.assertEqual(result["media_gaps"], 0)

    def test_game_with_screenshot_file_is_not_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            shot = Path(directory) / "s.png"
            shot.write_bytes(b"png")
            result = self._summary([{"name": "G", "path": "/x", "screenshots": [str(shot)]}])
            self.assertEqual(result["media_gaps"], 0)

    def test_game_without_media_is_a_gap(self):
        result = self._summary([{"name": "G", "path": "/x", "cover": "", "screenshots": []}])
        self.assertEqual(result["media_gaps"], 1)

    def test_game_has_media_rejects_non_dict_and_str_shots(self):
        from pkg.parity.parity_setup_preview import _game_has_media

        self.assertFalse(_game_has_media(None))
        self.assertFalse(_game_has_media({"screenshots": 42}))
        with tempfile.TemporaryDirectory() as directory:
            shot = Path(directory) / "s.png"
            shot.write_bytes(b"png")
            self.assertTrue(_game_has_media({"screenshots": str(shot)}))


class TestLaunchTokensPathAlias(unittest.TestCase):
    """#17: ``{Path}`` must substitute like ``{ImagePath}``/``{Dir}``."""

    def test_brace_path_token_resolves(self):
        game = {"name": "G", "path": "/roms/game.nes"}
        self.assertEqual(apply_tokens("emu {Path}", game), "emu /roms/game.nes")

    def test_path_token_is_known(self):
        self.assertIn("{Path}", PLACEHOLDERS)
        self.assertEqual(find_invalid_tokens("emu {Path}"), [])


class TestLaunchBoxMergeTarget(unittest.TestCase):
    """#18: a valid ``target_index`` must still honor ``target_game_id``."""

    def test_index_mismatch_falls_back_to_game_id(self):
        games = [{"game_id": "a", "name": "A"}, {"game_id": "b", "name": "B"}]
        operation = {"target_index": 0, "target_game_id": "b"}
        target = _find_target(games, operation)
        self.assertIs(target, games[1])

    def test_index_only_operations_still_resolve(self):
        games = [{"game_id": "a", "name": "A"}]
        self.assertIs(_find_target(games, {"target_index": 0}), games[0])
        self.assertIsNone(_find_target(games, {"target_index": 5}))

    def test_apply_plan_merges_into_id_matched_game(self):
        games = [{"game_id": "a", "name": "A"}, {"game_id": "b", "name": "B"}]
        plan = {
            "preview_token": "tok",
            "base_digest": _digest(games),
            "counts": {"found": 1},
            "operations": [
                {
                    "action": "merge",
                    "target_index": 0,
                    "target_game_id": "b",
                    "source_id": "src-1",
                    "changes": {"name": "B updated"},
                }
            ],
        }
        result = apply_import_plan(plan, games)
        self.assertEqual(result["games"][0]["name"], "A")
        self.assertEqual(result["games"][1]["name"], "B updated")
        self.assertEqual(result["counts"]["merged"], 1)

    def test_apply_plan_rejects_unresolvable_merge_target(self):
        games = [{"game_id": "a", "name": "A"}]
        plan = {
            "preview_token": "tok",
            "base_digest": _digest(games),
            "counts": {"found": 1},
            "operations": [
                {"action": "merge", "target_index": 9, "target_game_id": "ghost", "changes": {"name": "X"}}
            ],
        }
        with self.assertRaises(StaleImportPlan):
            apply_import_plan(plan, games)


class TestM3uWritePermissions(unittest.TestCase):
    """#19: ``.m3u`` writes into read-only ROM dirs degrade, not crash."""

    def _make_discs(self, root):
        (root / "Game (Disc 1).iso").write_bytes(b"1")
        (root / "Game (Disc 2).iso").write_bytes(b"2")

    def test_rom_dir_permission_denied_falls_back_to_generated_dir(self):
        import openbox

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roms"
            root.mkdir()
            self._make_discs(root)
            generated = Path(directory) / "app" / "generated"
            with mock.patch.object(openbox, "APP_DIR", Path(directory) / "app"):
                real_generate = parity_import.generate_m3u

                def deny_rom_dir(disc_paths, output_path):
                    if Path(output_path).parent == root:
                        raise PermissionError("read-only share")
                    return real_generate(disc_paths, output_path)

                with mock.patch.object(parity_import, "generate_m3u", side_effect=deny_rom_dir):
                    imported = parity_import.import_multi_platform(root, {".iso"}, {".iso": "Disc image"})
            self.assertTrue(str(imported[0]["path"]).endswith(".m3u"))
            self.assertTrue((generated / "Game.m3u").is_file())

    def test_all_targets_denied_falls_back_to_first_disc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_discs(root)
            with mock.patch.object(
                parity_import, "generate_m3u", side_effect=PermissionError("denied")
            ):
                imported = parity_import.import_multi_platform(root, {".iso"}, {".iso": "Disc image"})
            self.assertEqual(imported[0]["path"], str(root / "Game (Disc 1).iso"))
            self.assertEqual(len(imported[0]["discs"]), 2)

    def test_commit_preview_m3u_failure_keeps_first_disc(self):
        import openbox
        from state_store import JsonStateStore

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            disc1 = data_dir / "Game (Disc 1).iso"
            disc2 = data_dir / "Game (Disc 2).iso"
            disc1.write_bytes(b"1")
            disc2.write_bytes(b"2")
            with mock.patch.object(openbox, "APP_DIR", data_dir), mock.patch.object(
                openbox, "DATA", data_dir / "library.json"
            ), mock.patch.object(openbox, "STATE_STORE", JsonStateStore(data_dir / "library.json")):
                openbox.DATA.write_text(json.dumps({"schema_version": 6, "games": [], "settings": {}}))
                preview = create_preview_record(
                    sources=[{"type": "files", "id": "f1", "paths": [str(disc1)]}],
                    options={},
                    data_dir=data_dir,
                )
                preview["items"] = [
                    {
                        "candidate_id": "disc",
                        "group": "additions",
                        "source": {"type": "files", "id": "d", "label": "d", "path": str(disc1)},
                        "detected_title": "Game",
                        "detected_platform": "Disc image",
                        "intended_action": "import",
                        "existing_game_target": None,
                        "warnings": [],
                        "emulator_choices": [],
                        "selected_emulator_id": None,
                        "selected_adapter_id": None,
                        "launch_setup": None,
                        "merge_diff": None,
                        "_game": {"name": "Game", "platform": "Disc image", "path": str(disc1), "discs": [str(disc1), str(disc2)]},
                        "_identity": "path:" + str(disc1.resolve()),
                    }
                ]
                save_preview(preview, data_dir=data_dir)
                with mock.patch(
                    "pkg.parity.parity_setup_preview.revalidate_preview_record",
                    return_value=preview_document(preview),
                ), mock.patch(
                    "pkg.parity.parity_import.generate_m3u", side_effect=PermissionError("denied")
                ):
                    result = commit_preview(preview["preview_id"], revision=1, emulator_choices=[], data_dir=data_dir)
                self.assertEqual(result["added"], 1)
                state = json.loads(openbox.DATA.read_text())
                self.assertEqual(state["games"][0]["path"], str(disc1))


class _FakeMediaHandler:
    """Capture the handler surface ``MediaHandlers`` methods use."""

    def __init__(self):
        self.sent = []

    def send_json(self, status, payload, extra_headers=None):
        self.sent.append(("json", status, payload))

    def send_response(self, code, message=None):
        self.sent.append(("status", code))

    def headers_common(self, *args, **kwargs):
        pass

    def send_header(self, name, value):
        self.sent.append(("header", name, value))

    def end_headers(self):
        self.sent.append(("end",))


class TestEmuMoviesDownload(unittest.TestCase):
    """#23: EmuMovies downloads land on the field matching the media type."""

    def _handler(self):
        from handlers.media import MediaHandlers

        class Handler(_FakeMediaHandler, MediaHandlers):
            pass

        return Handler()

    def _run(self, payload, state):
        handler = self._handler()
        with mock.patch("handlers.media.load_emumovies_credentials", return_value={"username": "u", "password": "p"}), mock.patch(
            "handlers.media.load_state", return_value=state
        ), mock.patch(
            "handlers.media.download_emumovies_media", return_value="/media/emumovies/g/snap.jpg"
        ) as download, mock.patch(
            "handlers.media.transact_state", side_effect=lambda fn: (state, fn(state))
        ), mock.patch(
            "handlers.media.bump_media_epoch"
        ) as bump:
            handler.emumovies_download(payload)
        return handler, download, bump

    def test_box_type_sets_cover_and_bumps_epoch(self):
        state = {"games": [{"game_id": "g1", "name": "G", "path": "/roms/g.nes"}]}
        handler, _download, bump = self._run({"game_id": "g1"}, state)
        self.assertEqual(state["games"][0]["cover"], "/media/emumovies/g/snap.jpg")
        bump.assert_called_once_with()

    def test_snap_type_appends_to_screenshots_not_cover(self):
        state = {"games": [{"game_id": "g1", "name": "G", "path": "/roms/g.nes"}]}
        handler, download, bump = self._run({"game_id": "g1", "type": "snap"}, state)
        self.assertNotIn("cover", state["games"][0])
        self.assertEqual(state["games"][0]["screenshots"], ["/media/emumovies/g/snap.jpg"])
        download.assert_called_once()
        bump.assert_called_once_with()

    def test_unknown_media_type_rejected_before_download(self):
        state = {"games": [{"game_id": "g1", "name": "G", "path": "/roms/g.nes"}]}
        from handlers.media import MediaHandlers

        class Handler(_FakeMediaHandler, MediaHandlers):
            pass

        with mock.patch("handlers.media.load_emumovies_credentials", return_value={"u": "u", "p": "p"}), mock.patch(
            "handlers.media.load_state", return_value=state
        ), mock.patch("handlers.media.download_emumovies_media") as download:
            with self.assertRaises(ValueError):
                Handler().emumovies_download({"game_id": "g1", "type": "hologram"})
        download.assert_not_called()


class TestMediaRangeStatus(unittest.TestCase):
    """#24: malformed Range headers must surface 416, not a masked 404."""

    def test_send_file_value_error_maps_to_416(self):
        from handlers.media import MediaHandlers

        class Handler(_FakeMediaHandler, MediaHandlers):
            def send_file(self, status, path, content_type=None, extra_headers=None):
                raise ValueError("Invalid byte range.")

        handler = Handler()
        parsed = urllib.parse.urlparse("/api/media?id=0&kind=cover")
        with mock.patch(
            "handlers.media.load_state_view", return_value={"games": [{"name": "G", "cover": "/media/c.png"}]}
        ), mock.patch("handlers.media.approved_media_path", return_value=Path("/media/c.png")):
            handler._api_get_api_media(parsed)
        self.assertIn(("status", 416), handler.sent)
        self.assertIn(("header", "Content-Range", "bytes */0"), handler.sent)

    def test_live_server_malformed_range_returns_416(self):
        import web_app
        from openbox import save_state

        media_dir = _DATA_ROOT_PATH() / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / "cover.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        save_state({
            "games": [{"name": "G", "path": "/bin/true", "cover": str(target)}],
            "profiles": {},
            "history": [],
            "settings": {},
        })
        web_app.TOKEN = "sweep-residuals-token"
        server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/media?id=0&kind=cover&token=sweep-residuals-token",
                headers={"Range": "bytes=abc-"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            try:
                self.assertEqual(ctx.exception.code, 416)
                self.assertEqual(ctx.exception.headers.get("Content-Range"), f"bytes */{target.stat().st_size}")
            finally:
                ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestFrontendResiduals(unittest.TestCase):
    """#21/#22/#25/#26: static assertions matching test_frontend_contract style."""

    def _function_body(self, source, name):
        import re

        match = re.search(rf"function {name}\([^)]*\)\s*\{{", source)
        self.assertIsNotNone(match, f"missing function {name}")
        start = match.end()
        depth = 1
        index = start
        while index < len(source) and depth:
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
            index += 1
        return source[start:index - 1]

    def test_bigbox_video_snap_requires_has_video(self):
        js = (ROOT / "static" / "bigbox.js").read_text()
        body = self._function_body(js, "scheduleVideoSnap")
        self.assertIn("has_video", body)
        self.assertIn("media(game, 'video')", body)

    def test_bigbox_screensaver_requires_has_video(self):
        js = (ROOT / "static" / "bigbox.js").read_text()
        body = self._function_body(js, "startScreenSaver")
        self.assertIn("has_video", body)
        self.assertIn("has_cover", body)

    def test_reader_iframe_navigation_replaces_history(self):
        js = (ROOT / "static" / "reader.js").read_text()
        body = self._function_body(js, "setReaderPage")
        self.assertIn("location.replace", body)

    def test_app_css_defines_green_token(self):
        import re

        css = (ROOT / "static" / "app.css").read_text()
        root_block = re.search(r":root\s*\{[^}]*\}", css, re.DOTALL)
        self.assertIsNotNone(root_block)
        self.assertRegex(root_block.group(0), r"--green\s*:")
        for theme in sorted((ROOT / "themes").glob("*.css")):
            self.assertRegex(theme.read_text(), r"--green\s*:", theme.name)


def _DATA_ROOT_PATH():
    return Path(_DATA_ROOT.name)


if __name__ == "__main__":
    unittest.main()
