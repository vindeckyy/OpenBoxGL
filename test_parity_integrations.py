"""Tests for parity integration helpers."""

import json
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from parity_integrations import (
    auto_attach_obs_recording,
    download_bezel,
    download_emumovies_media,
    export_highscores,
    find_latest_recording,
    import_highscores,
    obs_recording_directory,
)


class IntegrationTests(unittest.TestCase):
    def test_find_latest_recording(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "old.mp4"
            newer = root / "new.mp4"
            older.write_bytes(b"a")
            newer.write_bytes(b"b")
            since = datetime.now() - timedelta(minutes=5)
            self.assertEqual(find_latest_recording(root, since=since), str(newer))

    def test_auto_attach_obs_recording(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "session.mp4"
            recording.write_bytes(b"video")
            game = {"name": "Demo", "video": ""}
            started = datetime.now() - timedelta(minutes=1)
            path = auto_attach_obs_recording(
                game,
                started,
                settings={"obs_auto_attach": True, "obs_recording_path": str(root)},
            )
            self.assertEqual(path, str(recording))
            self.assertEqual(game["video_recording"], str(recording))

    def test_highscore_export_and_import(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            hi = home / ".mame/hi"
            hi.mkdir(parents=True)
            (hi / "pacman.hi").write_bytes(b"score")
            game = {"name": "Pac-Man", "path": "/roms/pacman.zip", "rom_name": "pacman"}
            export_dir = home / "export"
            result = export_highscores(game, export_dir, home=home)
            self.assertTrue(Path(result["manifest"]).is_file())
            (hi / "pacman.hi").unlink()
            restored = import_highscores(game, export_dir, home=home)
            self.assertTrue(restored)
            self.assertTrue((hi / "pacman.hi").is_file())

    def test_emumovies_query_is_url_encoded(self):
        from urllib.parse import parse_qs, urlsplit
        game = {"name": "Rock & Roll Racing: Deluxe", "platform": "SNES"}
        captured = {}
        def fake_download_file(url, destination, **kwargs):
            captured["url"] = url
            Path(destination).write_bytes(b"img")
            return str(destination)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("parity_integrations.download_file", side_effect=fake_download_file):
                download_emumovies_media(game, {"username": "u", "password": "p"}, root)
        query = parse_qs(urlsplit(captured["url"]).query)
        self.assertEqual(query["search"][0], "Rock & Roll Racing: Deluxe")
        self.assertEqual(query["system"][0], "SNES")
        self.assertNotIn(" ", captured["url"])

    def test_download_bezel_preserves_existing_on_bad_archive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            # A valid first download.
            good_zip = root / "NES-bezels.zip"
            with zipfile.ZipFile(good_zip, "w") as package:
                package.writestr("bezels/bezel.png", b"good")
            def good_opener(request, timeout=0):
                return open(good_zip, "rb")
            first = download_bezel("NES", root, opener=good_opener)
            self.assertTrue(Path(first, "bezels", "bezel.png").is_file())
            # A corrupt second download must not destroy the existing bezels.
            with mock.patch("parity_integrations.safe_zip_extract", side_effect=ValueError("corrupt zip")):
                with self.assertRaises(ValueError):
                    download_bezel("NES", root, opener=good_opener)
            self.assertTrue(Path(first, "bezels", "bezel.png").is_file())


if __name__ == "__main__":
    unittest.main()
