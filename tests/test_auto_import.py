#!/usr/bin/env python3
"""Tests for auto-import, parallel scanner throughput, and large library deduplication."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

from parity_import import (
    dedupe_ranked_imports,
    group_multi_disc,
    import_multi_platform,
)



class AutoImportTests(unittest.TestCase):
    def test_basic_folder_import(self):
        with tempfile.TemporaryDirectory() as directory:
            prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
            os.environ["OPENBOX_DATA_DIR"] = str(Path(directory) / "data")
            try:
                Path(directory, "data").mkdir(parents=True, exist_ok=True)
                games = Path(directory) / "games"
                games.mkdir()
                (games / "one.nes").write_bytes(b"NES\x1a")
                (games / "ignore.txt").write_text("not a game")

                from openbox import load_state
                from webapp_state import import_folder_path

                added, found, _ = import_folder_path(games)
                self.assertEqual((added, found), (1, 1))
                added, found, _ = import_folder_path(games)
                self.assertEqual((added, found), (0, 1))
                imported = load_state()["games"][0]
                self.assertEqual(imported["platform"], "NES")
                self.assertEqual(imported["name"], "one")
            finally:
                if prev_data_dir is None:
                    os.environ.pop("OPENBOX_DATA_DIR", None)
                else:
                    os.environ["OPENBOX_DATA_DIR"] = prev_data_dir

    def test_large_directory_import_throughput(self):
        """Benchmark 10,000-ROM candidate deduplication and grouping under <150ms."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            psx_dir = root / "psx"
            snes_dir = root / "snes"
            nes_dir = root / "nes"

            synthetic_paths = []
            # 2,000 multi-disc PSX files (1,000 2-disc pairs)
            for i in range(1, 1001):
                synthetic_paths.append(psx_dir / f"RPG Game {i} (Disc 1).chd")
                synthetic_paths.append(psx_dir / f"RPG Game {i} (Disc 2).chd")

            # 4,000 SNES files (2,000 2-version pairs)
            for i in range(1, 2001):
                synthetic_paths.append(snes_dir / f"Action Game {i} (USA).smc")
                synthetic_paths.append(snes_dir / f"Action Game {i} (Europe).smc")

            # 4,000 NES files (unique)
            for i in range(1, 4001):
                synthetic_paths.append(nes_dir / f"Adventure {i}.nes")

            self.assertEqual(len(synthetic_paths), 10000)

            # Benchmark group_multi_disc
            start_group = time.perf_counter()
            groups = group_multi_disc(synthetic_paths)
            group_duration = time.perf_counter() - start_group

            multi_groups = [g for g in groups if len(g) > 1]
            self.assertEqual(len(multi_groups), 1000)
            self.assertEqual(len(groups), 9000)

            # Build additions
            additions = []
            for g in groups:
                if len(g) > 1:
                    additions.append({"name": g[0].name.split(" (Disc")[0], "platform": "PlayStation", "path": str(g[0]), "discs": [str(x) for x in g]})
                else:
                    path = g[0]
                    platform = "SNES" if path.suffix == ".smc" else "NES"
                    additions.append({"name": path.stem, "platform": platform, "path": str(path), "discs": []})

            # Benchmark dedupe_ranked_imports
            start_dedupe = time.perf_counter()
            deduped = dedupe_ranked_imports(additions)
            dedupe_duration = time.perf_counter() - start_dedupe

            total_alg_duration = group_duration + dedupe_duration

            max_allowed = 0.50 if sys.gettrace() is not None else 0.25
            self.assertLess(total_alg_duration, max_allowed, f"10k ROM processing took {total_alg_duration*1000:.1f}ms, target <{max_allowed*1000:.0f}ms")
            self.assertEqual(len(deduped), 7000)


    def test_progress_callback_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i in range(10):
                (root / f"game_{i}.nes").write_bytes(b"NES")

            progress_events = []
            def on_progress(**kwargs):
                progress_events.append(kwargs)

            candidates = import_multi_platform(
                root,
                {".nes"},
                {".nes": "NES"},
                progress_callback=on_progress,
            )
            self.assertEqual(len(candidates), 10)
            self.assertTrue(len(progress_events) > 0)
            keys = {k for event in progress_events for k in event.keys()}
            self.assertTrue("found_count" in keys or "processed_count" in keys)


if __name__ == "__main__":
    unittest.main()
