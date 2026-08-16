"""Regression tests for approved media and document paths."""

import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from unittest import mock

import webapp_state
from api_errors import MediaNotFound
from handlers.data import DataHandlers
from handlers.library import LibraryHandlers
from handlers.media import MediaHandlers


class MediaPathTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
