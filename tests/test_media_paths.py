"""Regression tests for approved media and document paths."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp_state
from api_errors import MediaNotFound
from handlers.data import DataHandlers
from handlers.library import LibraryHandlers
from handlers.media import MediaHandlers


class MediaPathTests(unittest.TestCase):
    def setUp(self):
        webapp_state.bump_media_epoch()
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tempdir.name) / "library.json"
        self.data_patch = mock.patch.object(webapp_state, "DATA", self.data_path)
        self.data_patch.start()
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("OPENBOX_MEDIA_ROOTS", None)

    def tearDown(self):
        self.env_patch.stop()
        self.data_patch.stop()
        self.tempdir.cleanup()
        webapp_state.bump_media_epoch()

    def test_media_and_documents_stay_under_managed_root(self):
        media = self.data_path.parent / "media" / "cover.png"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"cover")
        self.assertEqual(webapp_state.approved_media_path(media, must_exist=True), media.resolve())
        self.assertEqual(webapp_state.safe_document_file(media), media.resolve())
        with self.assertRaises(ValueError):
            webapp_state.approved_media_path("/etc/hosts")
        with self.assertRaises(FileNotFoundError):
            webapp_state.safe_document_file(self.data_path.parent / "media" / "missing.pdf")

    def test_explicit_external_root_is_opt_in(self):
        external = Path(self.tempdir.name) / "external-library"
        external.mkdir()
        cover = external / "cover.png"
        cover.write_bytes(b"cover")
        with self.assertRaises(ValueError):
            webapp_state.approved_media_path(cover)
        with mock.patch.dict(os.environ, {"OPENBOX_MEDIA_ROOTS": str(external)}):
            self.assertEqual(webapp_state.approved_media_path(cover, must_exist=True), cover.resolve())

    def test_symlinked_media_is_rejected(self):
        media = self.data_path.parent / "media"
        media.mkdir()
        link = media / "cover.png"
        link.symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            webapp_state.approved_media_path(link)

    def test_save_game_rejects_unapproved_media(self):
        handler = object.__new__(LibraryHandlers)
        handler.send_json = mock.Mock()
        with self.assertRaises(ValueError):
            handler.save_game({
                "game": {
                    "name": "Outside",
                    "path": "/bin/true",
                    "cover": "/etc/hosts",
                    "progress": "",
                    "rating": 0,
                }
            })

    def test_platform_document_save_rejects_unapproved_file(self):
        handler = object.__new__(DataHandlers)
        handler.send_json = mock.Mock()
        handler.clean_extras = LibraryHandlers.clean_extras
        with self.assertRaises(ValueError):
            handler.save_platform_documents({
                "platform": "PC",
                "documents": [{"name": "Hosts", "path": "/etc/hosts"}],
            })

    def test_media_handler_rejects_unapproved_file(self):
        handler = object.__new__(MediaHandlers)
        handler.send_file = mock.Mock()
        with mock.patch("handlers.media.load_state_view", return_value={"games": []}), \
             mock.patch("handlers.media.game_from_query", return_value={"cover": "/etc/hosts"}):
            with self.assertRaises(MediaNotFound):
                handler._api_get_api_media(urlparse("/api/media?kind=cover"))
        handler.send_file.assert_not_called()

    def test_file_probe_rejects_non_regular_files(self):
        fifo = Path(self.tempdir.name) / "media.fifo"
        os.mkfifo(fifo)
        self.assertFalse(webapp_state.probe_path(fifo, file_only=True))

    def test_public_state_selects_only_approved_video(self):
        video_root = self.data_path.parent / "media"
        video_root.mkdir(parents=True)
        approved = video_root / "theme.mp4"
        approved.write_bytes(b"video")
        state = {
            "games": [{
                "game_id": "game-1",
                "name": "Demo",
                "path": str(approved),
                "platform": "PC",
                "video_snap": "/etc/hosts",
                "video_theme": str(approved),
                "documents": [],
                "screenshots": [],
            }],
            "settings": {},
            "playlists": [],
        }
        with mock.patch.object(webapp_state, "load_state", return_value=state), \
             mock.patch.object(webapp_state, "load_state_readonly", return_value=state), \
             mock.patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
            game = webapp_state._build_public_state()["games"][0]
        self.assertEqual(game["active_video_field"], "video_theme")
        self.assertEqual(game["video_theme"], str(approved.resolve()))
        self.assertEqual(game["video_snap"], "")
        self.assertTrue(game["has_video"])

    def test_public_state_does_not_mutate_readonly_video_fields(self):
        video_root = self.data_path.parent / "media"
        video_root.mkdir(parents=True)
        approved = video_root / "snap.mp4"
        approved.write_bytes(b"video")
        state = {
            "games": [{
                "game_id": "game-legacy-video",
                "name": "Legacy Video",
                "path": "/bin/true",
                "platform": "PC",
                "video": str(approved),
                "documents": [],
                "screenshots": [],
            }],
            "settings": {},
            "playlists": [],
        }
        with mock.patch.object(webapp_state, "load_state", return_value=state), \
             mock.patch.object(webapp_state, "load_state_readonly", return_value=state), \
             mock.patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
            game = webapp_state._build_public_state()["games"][0]
        self.assertEqual(game["active_video_field"], "video_snap")
        self.assertNotIn("video_snap", state["games"][0])

    def test_find_duplicate_media_fast_path(self):
        from parity_media import cleanup_duplicates, find_duplicate_media
        media_dir = self.data_path.parent / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        f1 = media_dir / "cover1.png"
        f2 = media_dir / "cover2.png"
        f3 = media_dir / "unique.png"
        f1.write_bytes(b"identical_media_data")
        f2.write_bytes(b"identical_media_data")
        f3.write_bytes(b"different_size_data_here")
        games = [
            {"game_id": "g1", "cover": str(f1)},
            {"game_id": "g2", "cover": str(f2)},
            {"game_id": "g3", "cover": str(f3)},
        ]
        dupes = find_duplicate_media(games)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(len(dupes[0]["duplicates"]), 1)
        # Test dry-run cleanup vs real cleanup
        dry = cleanup_duplicates(dupes, dry_run=True)
        self.assertEqual(len(dry), 1)
        self.assertTrue(f2.is_file())
        deleted = cleanup_duplicates(dupes, dry_run=False, allowed_roots=[str(media_dir)])
        self.assertEqual(len(deleted), 1)
        self.assertFalse(f2.is_file())
        self.assertTrue(f1.is_file())

    def test_bulk_media_per_game_transaction(self):
        state = {
            "games": [
                {"game_id": "g1", "name": "Game 1", "launchbox_db_id": "1"},
                {"game_id": "g2", "name": "Game 2", "launchbox_db_id": "2"},
                {"game_id": "g3", "name": "Game 3", "launchbox_db_id": "3"},
            ],
            "settings": {},
        }
        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()

        transact_calls = []
        def fake_transact(mutator):
            transact_calls.append(mutator)
            mutator(state)
            return state, None

        with mock.patch("handlers.media.load_state", return_value=state), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", side_effect=lambda g, *a, **k: {**g, "cover": f"/media/{g['game_id']}.png"}), \
             mock.patch("handlers.media.transact_state", side_effect=fake_transact), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True

            def run_job(name, worker):
                worker()
            mock_jm.submit.side_effect = run_job

            handler.bulk_media({"media": ["cover"]})

        self.assertEqual(len(transact_calls), 3)
        self.assertEqual(state["games"][0]["cover"], "/media/g1.png")
        self.assertEqual(state["games"][1]["cover"], "/media/g2.png")
        self.assertEqual(state["games"][2]["cover"], "/media/g3.png")

    def test_bulk_media_retry_failed_skips_completed(self):
        state = {
            "games": [
                {"game_id": "g1", "name": "Game 1", "launchbox_db_id": "1"},
                {"game_id": "g2", "name": "Game 2", "launchbox_db_id": "2"},
            ],
            "settings": {},
        }
        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()
        apply_calls = []

        def fake_apply(game, *args, **kwargs):
            apply_calls.append(game["game_id"] if isinstance(game, dict) else game.get("game_id"))
            if str(game.get("game_id")) == "g2":
                raise ValueError("download failed")
            return {**game, "cover": "/media/c.png"}

        transact_calls = []
        def fake_transact(mutator):
            transact_calls.append(mutator)
            mutator(state)
            return state, None

        import handlers.media as hmed
        hmed.MEDIA_JOB.clear()
        hmed.MEDIA_JOB.update({
            "failed_game_ids": ["g2"],
            "completed_game_ids": ["g1"],
        })

        with mock.patch("handlers.media.load_state", return_value=state), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", side_effect=fake_apply), \
             mock.patch("handlers.media.transact_state", side_effect=fake_transact), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()

            handler.bulk_media({"media": ["cover"], "retry_failed": True})

        self.assertEqual(apply_calls, ["g2"])
        self.assertEqual(len(transact_calls), 0)

    def test_bulk_media_explicit_game_ids_and_no_change(self):
        state = {
            "games": [
                {"game_id": "g1", "name": "Game 1", "launchbox_db_id": "1"},
                {"game_id": "g2", "name": "Game 2", "launchbox_db_id": "2"},
            ],
            "settings": {},
        }
        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()

        with mock.patch("handlers.media.load_state", return_value=state), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", side_effect=lambda g, *a, **k: dict(g)), \
             mock.patch("handlers.media.transact_state") as mock_transact, \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()

            handler.bulk_media({"media": ["cover"], "game_ids": ["g2"]})

        mock_transact.assert_not_called()
        import handlers.media as hmed
        self.assertEqual(hmed.MEDIA_JOB.get("completed_game_ids"), ["g2"])

    def test_bulk_media_invalid_game_ids(self):
        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()
        with mock.patch("handlers.media.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = True
            with self.assertRaises(ValueError):
                handler.bulk_media({"media": ["cover"], "game_ids": "bad"})

    def test_bulk_media_validation_and_running_job(self):
        import handlers.media as hmed

        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()
        with mock.patch("handlers.media.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = False
            with self.assertRaises(ValueError):
                handler.bulk_media({"media": ["cover"]})
            mock_db.is_file.return_value = True
            with self.assertRaises(ValueError):
                handler.bulk_media({"media": []})
        hmed.MEDIA_JOB.clear()
        hmed.MEDIA_JOB.update({"state": "running", "current": 1, "total": 3})
        with mock.patch("handlers.media.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = True
            handler.bulk_media({"media": ["cover"]})
        handler.send_json.assert_called_with(200, hmed.MEDIA_JOB)

    def test_bulk_media_cancel_skip_and_manual_notes(self):
        import threading

        import handlers.media as hmed

        state = {
            "games": [
                {"game_id": "g1", "name": "Game 1", "launchbox_db_id": "1"},
                {"game_id": "g2", "name": "Game 2", "launchbox_db_id": "2"},
            ],
            "settings": {},
        }
        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()
        cancel = threading.Event()
        transact_calls = []

        def fake_transact(mutator):
            transact_calls.append(mutator)
            mutator(state)
            return state, None

        def fake_apply(game, *args, **kwargs):
            game_id = str(game.get("game_id"))
            if game_id == "g1":
                cancel.set()
                return {**game, "cover": "/media/g1.png"}
            return {**game, "cover": "/media/g2.png", "_media_notes": ["manual: no manual in this archive"]}

        hmed.MEDIA_JOB.clear()

        with mock.patch("handlers.media.load_state", return_value=state), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", side_effect=fake_apply), \
             mock.patch("handlers.media.transact_state", side_effect=fake_transact), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm, \
             mock.patch("handlers.media.bump_media_epoch"):
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker(cancel)
            handler.bulk_media({"media": ["cover", "manual"]})

        self.assertEqual(len(transact_calls), 1)
        self.assertEqual(hmed.MEDIA_JOB.get("manual_missing"), 0)

        hmed.MEDIA_JOB.clear()
        hmed.MEDIA_JOB.update({"failed_game_ids": ["g1", "g2"], "completed_game_ids": ["g2"]})
        transact_calls.clear()

        with mock.patch("handlers.media.load_state", return_value=state), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", side_effect=lambda g, *a, **k: {**g, "cover": "/media/x.png"}), \
             mock.patch("handlers.media.transact_state", side_effect=fake_transact), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm, \
             mock.patch("handlers.media.bump_media_epoch"):
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler.bulk_media({"media": ["cover"], "retry_failed": True})

        self.assertEqual(len(transact_calls), 1)
        self.assertEqual(hmed.MEDIA_JOB.get("completed_game_ids"), ["g1", "g2"])

        hmed.MEDIA_JOB.clear()
        transact_calls.clear()

        with mock.patch("handlers.media.load_state", return_value=state), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", side_effect=lambda g, *a, **k: {**g, "_media_notes": ["manual: no manual in this archive"]}), \
             mock.patch("handlers.media.transact_state", side_effect=fake_transact), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm, \
             mock.patch("handlers.media.bump_media_epoch"):
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler.bulk_media({"media": ["manual"], "game_ids": ["g1"]})

        self.assertEqual(hmed.MEDIA_JOB.get("manual_missing"), 1)

    def test_match_metadata_batched_transaction(self):
        from handlers.metadata import MetadataHandlers
        state = {
            "games": [
                {"game_id": "g1", "name": "Game A"},
                {"game_id": "g2", "name": "Game B"},
            ],
            "settings": {},
        }
        handler = object.__new__(MetadataHandlers)
        handler.send_json = mock.Mock()

        transact_calls = []
        def fake_transact(mutator):
            transact_calls.append(mutator)
            mutator(state)
            return state, None

        with mock.patch("handlers.metadata.load_state", return_value=state), \
             mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.metadata.batch_match", return_value={("Game A", ""): {"database_id": 10}, ("Game B", ""): {"database_id": 20}}), \
             mock.patch("handlers.metadata.transact_state", side_effect=fake_transact), \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()

            handler.match_metadata({"platform": "all"})

        # Single batch transaction for all matched games
        self.assertEqual(len(transact_calls), 1)
        self.assertEqual(state["games"][0]["launchbox_db_id"], "10")
        self.assertEqual(state["games"][1]["launchbox_db_id"], "20")

    def test_match_metadata_and_bulk_media_error_handling(self):
        from handlers.metadata import MetadataHandlers
        from handlers.media import MediaHandlers
        import handlers.metadata as hm
        import handlers.media as hmed

        # Test match_metadata error in mutate and transact_state
        handler_meta = object.__new__(MetadataHandlers)
        handler_meta.send_json = mock.Mock()
        state = {"games": [{"game_id": "g1", "name": "Game A"}], "settings": {}}

        with mock.patch("handlers.metadata.load_state", return_value=state), \
             mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.metadata.batch_match", return_value={("Game A", ""): {"database_id": 10}}), \
             mock.patch("handlers.metadata.transact_state", side_effect=RuntimeError("db error")), \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler_meta.match_metadata({"platform": "all"})
            self.assertTrue(any("Batch state update error" in err for err in hm.METADATA_JOB.get("errors", [])))

        # Test mutate inner KeyError handling
        with mock.patch("handlers.metadata.load_state", return_value=state), \
             mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.metadata.batch_match", return_value={("Game A", ""): {"database_id": 10}}), \
             mock.patch("handlers.metadata.transact_state", side_effect=lambda mutator: mutator({"games": []})), \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler_meta.match_metadata({"platform": "all"})
            self.assertTrue(any("Game A" in err for err in hm.METADATA_JOB.get("errors", [])))

        # Test bulk_media error in transact_state
        state_media = {"games": [{"game_id": "g1", "name": "Game A", "launchbox_db_id": "100"}], "settings": {}}
        handler_media = object.__new__(MediaHandlers)
        handler_media.send_json = mock.Mock()
        hmed.MEDIA_JOB.clear()
        with mock.patch("handlers.media.load_state", return_value=state_media), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", return_value={"game_id": "g1", "cover": "/media/c.png"}), \
             mock.patch("handlers.media.transact_state", side_effect=RuntimeError("transact error")), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler_media.bulk_media({"media": ["cover"]})
            self.assertIn("g1", hmed.MEDIA_JOB.get("failed_game_ids", []))
            self.assertTrue(any("transact error" in err for err in hmed.MEDIA_JOB.get("errors", [])))

        # Test bulk_media mutate inner KeyError
        with mock.patch("handlers.media.load_state", return_value=state_media), \
             mock.patch("handlers.media.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.media.apply_game_metadata", return_value={"game_id": "g1", "cover": "/media/c.png"}), \
             mock.patch("handlers.media.transact_state", side_effect=lambda mutator: mutator({"games": []})), \
             mock.patch("handlers.media.JOB_MANAGER") as mock_jm:
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler_media.bulk_media({"media": ["cover"]})

    def test_active_video_legacy_and_edge_cases(self):
        from parity_media import active_video, find_duplicate_media
        # Legacy video field
        media = self.data_path.parent / "media" / "legacy.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"vid")
        
        # Valid legacy video
        game1 = {"video": str(media)}
        field, path = active_video(game1)
        self.assertEqual(field, "video_snap")
        self.assertEqual(path, str(media))

        # Unapproved legacy video
        game2 = {"video": "/etc/hosts"}
        field, path = active_video(game2)
        self.assertEqual(field, "")
        self.assertEqual(path, "")

        # Exception in sanitize_media_path during active_video
        with mock.patch("webapp_state.sanitize_media_path", side_effect=Exception("sanitize fail")):
            field, path = active_video({"video_theme": str(media), "video": str(media)})
            self.assertEqual(field, "")
            self.assertEqual(path, "")

        # OSError in find_duplicate_media stat size check
        orig_stat = Path.stat
        calls = [0]
        def selective_stat(self, *args, **kwargs):
            calls[0] += 1
            if calls[0] > 2:
                raise OSError("stat error")
            return orig_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", selective_stat):
            dupes = find_duplicate_media([{"cover": str(media)}])
            self.assertEqual(dupes, [])


if __name__ == "__main__":
    unittest.main()

