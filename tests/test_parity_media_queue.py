#!/usr/bin/env python3
"""Durable media queue regression tests (P8.2)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

import pkg.parity  # noqa: F401,E402  # register flat-import finder

from parity_media import (
    MEDIA_QUEUE_BACKOFF,
    enqueue_media_job,
    load_media_queue,
    load_normalized_media_queue,
    media_queue_snapshot,
    media_queue_worker,
    normalize_media_queue_item,
    process_media_queue,
    retry_media_queue_item,
    save_media_queue,
)


def _state(games, **settings):
    base = {"media_download_limit": 0, "media_download_limit_per_group": 0}
    base.update(settings)
    return {"games": games, "settings": base}


def _game(game_id, name="Game", platform="PC", **extra):
    game = {"game_id": game_id, "name": name, "platform": platform}
    game.update(extra)
    return game


class QueueNormalizationTests(unittest.TestCase):
    def test_legacy_item_gets_identity_and_state(self):
        item = normalize_media_queue_item({
            "name": "Legacy",
            "path": "/roms/legacy.zip",
            "game_id": "g1",
            "media": ["cover", "video_snap"],
        })
        self.assertTrue(item["id"].startswith("mq-"))
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(item["state"], "queued")
        self.assertEqual(item["kind"], "cover")
        self.assertEqual(item["media"], ["cover", "video_snap"])
        self.assertEqual(item["source"], "launchbox")
        self.assertFalse(item["overwrite"])

    def test_legacy_load_normalizes_without_mutating_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "media-queue.json"
            save_media_queue(queue, [{"game_id": "g1", "media": ["cover"]}])
            self.assertEqual(load_media_queue(queue), [{"game_id": "g1", "media": ["cover"]}])
            items = load_normalized_media_queue(queue)
            self.assertEqual(len(items), 1)
            self.assertIn("id", items[0])
            self.assertEqual(load_media_queue(queue), [{"game_id": "g1", "media": ["cover"]}])

    def test_invalid_source_rejected(self):
        with self.assertRaises(ValueError):
            normalize_media_queue_item({"game_id": "g1", "source": "ftp", "kind": "cover"})


class EnqueueTests(unittest.TestCase):
    def test_duplicate_replaces_unless_overwrite_differs(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "media-queue.json"
            enqueue_media_job(queue, {"game_id": "g1", "source": "launchbox", "kind": "cover", "name": "One"})
            enqueue_media_job(queue, {"game_id": "g1", "source": "launchbox", "kind": "cover", "name": "Two"})
            items = load_media_queue(queue)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["name"], "Two")
            enqueue_media_job(queue, {
                "game_id": "g1", "source": "launchbox", "kind": "cover", "name": "Three", "overwrite": True,
            })
            self.assertEqual(len(load_media_queue(queue)), 2)

    def test_legacy_media_list_splits_per_kind(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "media-queue.json"
            enqueue_media_job(queue, {"game_id": "g1", "media": ["cover", "video_snap", "manual"]})
            items = load_normalized_media_queue(queue)
            self.assertEqual({item["kind"] for item in items}, {"cover", "video_snap", "manual"})
            self.assertEqual(len(items), 3)

    def test_cap_enforced(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "media-queue.json"
            with mock.patch("parity_media.MEDIA_QUEUE_MAX", 2):
                enqueue_media_job(queue, {"game_id": "g1", "kind": "cover"})
                enqueue_media_job(queue, {"game_id": "g2", "kind": "cover"})
                with self.assertRaises(ValueError):
                    enqueue_media_job(queue, {"game_id": "g3", "kind": "cover"})
            self.assertEqual(len(load_media_queue(queue)), 2)


class ImportQueueTests(unittest.TestCase):
    def test_enqueue_import_media_splits_kinds_and_counts_games(self):
        from pkg.state.imports import enqueue_import_media

        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "media-queue.json"
            queued = enqueue_import_media(
                [
                    {"game_id": "g1", "name": "One", "launchbox_db_id": 1},
                    {"game_id": "g2", "name": "Two", "metadata_provider": "igdb"},
                    {"game_id": "g3", "metadata_provider": "unsupported"},
                    {"name": "No id", "launchbox_db_id": 2},
                ],
                {"auto_import_media_types": ["cover", "video_snap"]},
                queue_path=queue,
            )
            self.assertEqual(queued, 2)
            items = load_normalized_media_queue(queue)
            self.assertEqual({item["source"] for item in items}, {"launchbox", "igdb"})
            self.assertEqual({item["kind"] for item in items}, {"cover", "video_snap"})
            self.assertEqual({item["game_id"] for item in items}, {"g1", "g2"})

    def test_enqueue_import_media_honors_game_limit(self):
        from pkg.state.imports import enqueue_import_media

        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "media-queue.json"
            queued = enqueue_import_media(
                [
                    {"game_id": "g1", "launchbox_db_id": 1},
                    {"game_id": "g2", "launchbox_db_id": 2},
                ],
                {"auto_import_media_types": ["cover"], "media_download_limit": 1},
                queue_path=queue,
            )
            self.assertEqual(queued, 1)
            self.assertEqual([item["game_id"] for item in load_normalized_media_queue(queue)], ["g1"])

    def test_merge_imported_games_queues_media_for_new_games(self):
        from pkg.state import imports as state_imports

        with tempfile.TemporaryDirectory() as folder:
            data_file = Path(folder) / "data" / "library.json"
            data_file.parent.mkdir(parents=True)
            queue = data_file.parent / "media-queue.json"
            library = {"games": [], "settings": {"auto_import_media_types": ["cover"]}}

            def update_state(mutator):
                mutator(library)
                for index, game in enumerate(library["games"], 1):
                    game.setdefault("game_id", f"g{index}")
                return library

            with mock.patch.object(state_imports, "DATA", data_file), \
                 mock.patch.object(state_imports, "update_state", side_effect=update_state):
                added, found = state_imports.merge_imported_games(
                    [{
                        "name": "Zelda",
                        "path": "/roms/zelda.zip",
                        "platform": "SNES",
                        "launchbox_db_id": 42,
                    }],
                    lambda game: ("path", game.get("path", "")),
                )

            self.assertEqual((added, found), (1, 1))
            items = load_normalized_media_queue(queue)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["game_id"], "g1")
            self.assertEqual(items[0]["source"], "launchbox")
            self.assertEqual(items[0]["kind"], "cover")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.queue = Path(self.tempdir.name) / "media-queue.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_retry_backoff_and_failed_state(self):
        state = _state([_game("g1", cover="")])
        enqueue_media_job(self.queue, {"game_id": "g1", "source": "launchbox", "kind": "cover"})
        clock = [1000.0]

        def boom(game, kind, item, current, data_dir):
            raise ValueError("provider down")

        result = process_media_queue(self.queue, state=state, download=boom, now=lambda: clock[0])
        self.assertEqual(result["failed"], 0)
        items = load_normalized_media_queue(self.queue)
        self.assertEqual(items[0]["attempts"], 1)
        self.assertEqual(items[0]["state"], "queued")
        self.assertEqual(items[0]["next_attempt_at"], 1000.0 + MEDIA_QUEUE_BACKOFF[0])
        self.assertIn("provider down", items[0]["last_error"])

        # Not due yet: nothing runs.
        result = process_media_queue(self.queue, state=state, download=boom, now=lambda: clock[0])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(load_normalized_media_queue(self.queue)[0]["attempts"], 1)

        clock[0] += MEDIA_QUEUE_BACKOFF[0] + 1
        process_media_queue(self.queue, state=state, download=boom, now=lambda: clock[0])
        items = load_normalized_media_queue(self.queue)
        self.assertEqual(items[0]["attempts"], 2)
        self.assertEqual(items[0]["next_attempt_at"], clock[0] + MEDIA_QUEUE_BACKOFF[1])

        clock[0] += MEDIA_QUEUE_BACKOFF[1] + 1
        result = process_media_queue(self.queue, state=state, download=boom, now=lambda: clock[0])
        items = load_normalized_media_queue(self.queue)
        self.assertEqual(items[0]["attempts"], 3)
        self.assertEqual(items[0]["state"], "failed")
        self.assertEqual(result["failed"], 1)

    def test_retry_item_resets_failed_state(self):
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover"})
        items = load_normalized_media_queue(self.queue)
        item_id = items[0]["id"]
        items[0].update({"attempts": 3, "state": "failed", "last_error": "x"})
        save_media_queue(self.queue, items)
        self.assertTrue(retry_media_queue_item(self.queue, item_id))
        reset = load_normalized_media_queue(self.queue)[0]
        self.assertEqual((reset["attempts"], reset["state"], reset["last_error"]), (0, "queued", ""))

    def test_existing_media_preserved_without_overwrite(self):
        state = _state([_game("g1", cover="/media/old.jpg")])
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover", "overwrite": False})
        calls = []

        def downloader(game, kind, item, current, data_dir):
            calls.append(kind)
            return "/media/new.jpg"

        result = process_media_queue(self.queue, state=state, download=downloader)
        self.assertEqual(calls, [])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(state["games"][0]["cover"], "/media/old.jpg")
        self.assertEqual(load_media_queue(self.queue), [])

    def test_overwrite_replaces_cover(self):
        state = _state([_game("g1", cover="/media/old.jpg")])
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover", "overwrite": True})
        process_media_queue(
            self.queue, state=state,
            download=lambda game, kind, item, current, data_dir: "/media/new.jpg",
        )
        self.assertEqual(state["games"][0]["cover"], "/media/new.jpg")

    def test_manual_never_replaced_even_with_overwrite(self):
        state = _state([_game("g1", manual="/media/old.pdf")])
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "manual", "overwrite": True})
        calls = []
        process_media_queue(
            self.queue, state=state,
            download=lambda game, kind, item, current, data_dir: calls.append(kind) or "/media/new.pdf",
        )
        self.assertEqual(calls, [])
        self.assertEqual(state["games"][0]["manual"], "/media/old.pdf")

    def test_global_limit_counts_games(self):
        state = _state(
            [_game("g1", cover=""), _game("g2", cover="")],
            media_download_limit=1,
        )
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover"})
        enqueue_media_job(self.queue, {"game_id": "g2", "kind": "cover"})
        result = process_media_queue(
            self.queue, state=state,
            download=lambda game, kind, item, current, data_dir: f"/media/{game['game_id']}.jpg",
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(load_media_queue(self.queue)), 1)
        self.assertEqual(state["games"][0]["cover"], "/media/g1.jpg")
        self.assertEqual(state["games"][1]["cover"], "")

    def test_per_group_limit_leaves_rest_queued(self):
        state = _state(
            [_game("g1", platform="SNES", cover=""), _game("g2", platform="SNES", cover="")],
            media_download_limit_per_group=1,
        )
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover"})
        enqueue_media_job(self.queue, {"game_id": "g2", "kind": "cover"})
        result = process_media_queue(
            self.queue, state=state,
            download=lambda game, kind, item, current, data_dir: f"/media/{game['game_id']}.jpg",
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(load_media_queue(self.queue)), 1)

    def test_pause_and_cancel_leave_queue_untouched(self):
        state = _state([_game("g1", cover="")])
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover"})
        pause = __import__("threading").Event()
        pause.set()
        result = process_media_queue(self.queue, state=state, pause_event=pause)
        self.assertTrue(result["paused"])
        self.assertEqual(len(load_media_queue(self.queue)), 1)

        cancel = __import__("threading").Event()
        cancel.set()
        result = process_media_queue(self.queue, state=state, cancel_event=cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(load_media_queue(self.queue)), 1)

    def test_worker_exits_on_cancel(self):
        state = _state([_game("g1", cover="")])
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover"})
        cancel = __import__("threading").Event()
        cancel.set()
        self.assertTrue(
            media_queue_worker(queue_path=self.queue, state=state, cancel_event=cancel)["cancelled"]
        )
        self.assertEqual(len(load_media_queue(self.queue)), 1)

    def test_snapshot_shape(self):
        enqueue_media_job(self.queue, {"game_id": "g1", "kind": "cover"})
        snapshot = media_queue_snapshot(self.queue)
        self.assertEqual(snapshot["items"], snapshot["queue"])
        self.assertEqual(snapshot["failed"], 0)
        self.assertEqual(snapshot["counts"], {
            "total": 1, "queued": 1, "failed": 0, "completed": 0,
        })


if __name__ == "__main__":
    unittest.main()
