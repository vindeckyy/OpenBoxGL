#!/usr/bin/env python3
"""P2-6: bulk media download commits one transaction for the whole batch."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = tempfile.mkdtemp(prefix="openbox-media-batch-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_DIR

import handlers.media as media_module  # noqa: E402
from handlers.media import MediaHandlers  # noqa: E402
from pkg.state import media_probe  # noqa: E402
from pkg.state.cache import clear_file_probe_cache  # noqa: E402


class MediaProbeBatchTests(unittest.TestCase):
    def test_batch_probe_stats_each_unique_path_once(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "cover.png"
            existing.write_bytes(b"png")
            missing = Path(directory) / "gone.png"
            clear_file_probe_cache()
            with mock.patch.object(media_probe.os, "stat", wraps=media_probe.os.stat) as stat_spy:
                first = media_probe.probe_paths_batch([str(existing), str(existing), str(missing), ""])
                stats_after_first = stat_spy.call_count
                second = media_probe.probe_paths_batch([str(existing), str(missing)])
                stats_after_second = stat_spy.call_count
            self.assertEqual(first[str(existing)], True)
            self.assertEqual(first[str(missing)], False)
            self.assertEqual(second[str(existing)], True)
            self.assertEqual(stats_after_second, stats_after_first, "cached paths must not be re-stat'ed")

    def test_media_probe_paths_batch_rejects_unapproved_paths(self):
        clear_file_probe_cache()
        results = media_probe.media_probe_paths_batch(["/etc/passwd", "/tmp/not-media.png"])
        self.assertEqual(results, {"/etc/passwd": False, "/tmp/not-media.png": False})


class BulkMediaBatchTests(unittest.TestCase):
    def setUp(self):
        media_module.MEDIA_JOB.clear()
        self.games = [
            {
                "game_id": f"g{index:03d}",
                "name": f"Game {index}",
                "launchbox_db_id": str(1000 + index),
                "platform": "PC",
                "path": f"/roms/{index}",
            }
            for index in range(200)
        ]

    def test_two_hundred_matches_write_state_once(self):
        database = Path(_DATA_DIR) / "metadata.sqlite"
        database.write_bytes(b"stub")
        writes = []
        state = {"games": self.games, "settings": {}}

        def fake_transact(mutator):
            writes.append(1)
            mutator(state)
            return state, None

        def fake_metadata(game, _database, database_id, _media_types, _root, _overwrite):
            updated = dict(game)
            updated["cover"] = f"/media/launchbox/{database_id}.png"
            return updated

        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()
        with mock.patch.object(media_module, "METADATA_DATABASE", database), \
             mock.patch.object(media_module, "load_state", return_value=state), \
             mock.patch.object(media_module, "apply_game_metadata", side_effect=fake_metadata), \
             mock.patch.object(media_module, "transact_state", side_effect=fake_transact), \
             mock.patch.object(media_module, "bump_media_epoch"), \
             mock.patch.object(
                 media_module.JOB_MANAGER, "submit",
                 side_effect=lambda _name, worker: (worker(), {"job_id": "test-job", "state": "done"})[1],
             ):
            handler.bulk_media({"media": ["cover"], "platform": "all"})

        self.assertEqual(len(writes), 1, "the whole batch must commit exactly one transaction")
        self.assertEqual(media_module.MEDIA_JOB["state"], "done")
        self.assertEqual(media_module.MEDIA_JOB["updated"], 200)
        self.assertEqual(media_module.MEDIA_JOB["total"], 200)
        self.assertTrue(all(game.get("cover") for game in self.games))
        handler.send_json.assert_called_once_with(202, {"state": "running"})


if __name__ == "__main__":
    unittest.main()
