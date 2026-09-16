#!/usr/bin/env python3
"""P1-9/P1-10: concurrent extraction safety and a bounded extraction cache."""

import os
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import archives
from archives import extract_game, extraction_dir, prune_extraction_cache


def _write_zip(path, payload):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("game.rom", payload)


class ConcurrentExtractionTests(unittest.TestCase):
    def test_concurrent_extraction_keeps_winner_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "game.zip"
            _write_zip(archive, b"payload")
            cache = root / "cache"
            started = threading.Barrier(2)
            results = []
            errors = []
            original = archives.safe_zip_extract

            def slow_extract(*args, **kwargs):
                time.sleep(0.2)
                return original(*args, **kwargs)

            def worker():
                try:
                    started.wait(timeout=10)
                    results.append(extract_game(archive, cache))
                except Exception as error:
                    errors.append(error)

            with mock.patch.object(archives, "safe_zip_extract", side_effect=slow_extract) as patched:
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertEqual(errors, [])
                self.assertEqual(patched.call_count, 1)

            self.assertEqual(len(results), 2)
            for selected in results:
                self.assertTrue(selected.is_file())
                self.assertEqual(selected.read_bytes(), b"payload")
            destination = extraction_dir(archive, cache)
            self.assertTrue(destination.is_dir())
            self.assertTrue((destination / ".complete").is_file())
            self.assertEqual(destination / "game.rom", results[0])

    def test_extraction_keeps_tree_promoted_by_another_process(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "game.zip"
            _write_zip(archive, b"payload")
            cache = root / "cache"
            destination = extraction_dir(archive, cache)

            def promote_during_extraction(_source, _staging):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "game.rom").write_bytes(b"winner")
                (destination / ".complete").touch()

            with mock.patch.object(archives, "safe_zip_extract", side_effect=promote_during_extraction):
                selected = extract_game(archive, cache)

            self.assertEqual(selected.read_bytes(), b"winner")
            self.assertEqual(destination / "game.rom", selected)
            self.assertEqual([item for item in cache.iterdir() if item.name.startswith(".")], [])

    def test_extraction_swallows_replace_race_that_left_a_winner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "game.zip"
            _write_zip(archive, b"payload")
            cache = root / "cache"
            destination = extraction_dir(archive, cache)
            original_replace = Path.replace

            def racing_replace(self, target):
                if target == destination:
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "game.rom").write_bytes(b"winner")
                    (destination / ".complete").touch()
                    raise OSError("raced")
                return original_replace(self, target)

            with mock.patch.object(Path, "replace", racing_replace):
                selected = extract_game(archive, cache)

            self.assertEqual(selected.read_bytes(), b"winner")

    def test_extraction_raises_replace_error_without_a_winner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "game.zip"
            _write_zip(archive, b"payload")
            cache = root / "cache"

            with mock.patch.object(Path, "replace", side_effect=OSError("no winner")):
                with self.assertRaises(OSError):
                    extract_game(archive, cache)

            self.assertEqual([item for item in cache.iterdir() if item.name.startswith(".")], [])


class PruneExtractionCacheTests(unittest.TestCase):
    def _make_tree(self, root, name, mtime):
        tree = root / name
        tree.mkdir()
        (tree / "game.rom").write_bytes(b"x")
        (tree / ".complete").touch()
        os.utime(tree, (mtime, mtime))
        return tree

    def test_prune_keeps_newest_trees_and_skips_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = self._make_tree(root, "a" * 20, 1_000)
            older = self._make_tree(root, "b" * 20, 500)
            newest = self._make_tree(root, "c" * 20, 5_000)
            staging = root / ".d.extracting-123"
            staging.mkdir()
            snapshot = root / ".openbox-archive-tmp"
            snapshot.write_bytes(b"tmp")

            removed = prune_extraction_cache(root, keep=2)

            self.assertEqual(removed, 1)
            self.assertFalse(older.exists())
            self.assertTrue(old.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(staging.exists())
            self.assertTrue(snapshot.exists())

    def test_prune_env_override_bounds_the_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                self._make_tree(root, f"{index:020x}", 1_000 + index)
            with mock.patch.dict(os.environ, {"OPENBOX_ARCHIVE_CACHE_MAX_TREES": "1"}):
                self.assertEqual(archives._archive_cache_limit(), 1)
                removed = prune_extraction_cache(root)
            self.assertEqual(removed, 2)
            self.assertEqual(len([path for path in root.iterdir() if path.is_dir()]), 1)

    def test_prune_invalid_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"OPENBOX_ARCHIVE_CACHE_MAX_TREES": "many"}):
            self.assertEqual(archives._archive_cache_limit(), archives.MAX_ARCHIVE_CACHE_TREES)

    def test_prune_missing_root_is_a_noop(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(prune_extraction_cache(Path(directory) / "missing"), 0)

    def test_prune_skips_incomplete_trees_and_keeps_current(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = self._make_tree(root, "a" * 20, 1_000)
            self._make_tree(root, "b" * 20, 2_000)
            incomplete = root / ("c" * 20)
            incomplete.mkdir()
            (incomplete / "partial.rom").write_bytes(b"x")

            removed = prune_extraction_cache(root, keep=1, keep_current=old)

            self.assertEqual(removed, 0)
            self.assertTrue(old.exists())
            self.assertTrue(incomplete.exists())
            self.assertEqual(len([path for path in root.iterdir() if path.is_dir()]), 3)

    def test_extract_game_survives_prune_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "game.zip"
            _write_zip(archive, b"payload")
            cache = root / "cache"
            with mock.patch.object(archives, "prune_extraction_cache", side_effect=OSError("denied")):
                selected = extract_game(archive, cache)
            self.assertEqual(selected.read_bytes(), b"payload")

    def test_extract_game_prunes_old_trees(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.zip"
            second = root / "second.zip"
            _write_zip(first, b"first")
            _write_zip(second, b"second")
            cache = root / "cache"
            with mock.patch.dict(os.environ, {"OPENBOX_ARCHIVE_CACHE_MAX_TREES": "1"}):
                extract_game(first, cache)
                extract_game(second, cache)
            self.assertFalse(extraction_dir(first, cache).exists())
            self.assertTrue(extraction_dir(second, cache).exists())


if __name__ == "__main__":
    unittest.main()
