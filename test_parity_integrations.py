"""Tests for parity integration helpers."""

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from parity_integrations import (
    auto_attach_obs_recording,
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


if __name__ == "__main__":
    unittest.main()
