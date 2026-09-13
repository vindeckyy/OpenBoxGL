"""Tests for deterministic reel manifests and optional ffmpeg rendering."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_reels as reels  # noqa: E402


class ReelTests(unittest.TestCase):
    def _media(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        cover = root / "cover.jpg"
        clip_a = root / "a.mp4"
        clip_b = root / "b.mp4"
        moment = root / "moment.png"
        for path in (cover, clip_a, clip_b, moment):
            path.write_bytes(path.name.encode())
        return root, cover, clip_a, clip_b, moment

    def test_manifest_is_deterministic_and_sorted(self):
        _root, cover, clip_a, clip_b, moment = self._media()
        first = reels.build_reel_manifest(
            {"game_id": "zelda/1", "name": "Zelda"},
            clips=[{"path": str(clip_b), "created_at": "2026-02-02"},
                   {"path": str(clip_a), "created_at": "2026-01-02"},
                   {"path": "/missing.mp4"}],
            moments=[{"screenshot": str(moment), "created_at": "2026-03-01", "note": "boss"}],
            cover=str(cover),
        )
        second = reels.build_reel_manifest(
            {"game_id": "zelda/1", "name": "Zelda"},
            clips=[{"path": str(clip_a), "created_at": "2026-01-02"},
                   {"path": str(clip_b), "created_at": "2026-02-02"}],
            moments=[{"screenshot": str(moment), "created_at": "2026-03-01", "note": "boss"}],
            cover=str(cover),
        )
        self.assertEqual(first, second)
        self.assertEqual([item["kind"] for item in first["inputs"]], ["cover", "clip", "clip", "moment"])
        self.assertEqual([Path(item["path"]).name for item in first["inputs"]], [
            "cover.jpg", "a.mp4", "b.mp4", "moment.png",
        ])
        self.assertEqual(json.loads(reels.manifest_json(first)), first)

    def test_write_manifest_is_stable_and_reel_path_is_safe(self):
        root, cover, _clip_a, _clip_b, _moment = self._media()
        manifest = reels.build_manifest([], [], str(cover), game_id="A game/1")
        destination = root / "reels" / "entry.manifest.json"
        self.assertEqual(reels.write_manifest(manifest, destination), str(destination))
        self.assertEqual(json.loads(destination.read_text()), manifest)
        self.assertEqual(reels.reel_path(root, game_id="A game/1"), root / "reels" / "A-game-1.mp4")
        self.assertEqual(reels.reel_path(root, year=2026), root / "reels" / "2026.mp4")

    def test_ffmpeg_present_uses_mp4_output_path(self):
        root, cover, clip_a, _clip_b, _moment = self._media()
        manifest = reels.build_reel_manifest(game_id="demo", clips=[str(clip_a)], cover=str(cover))

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"fake mp4")
            self.assertTrue(kwargs["check"])
            self.assertIn("-filter_complex", command)
            return mock.Mock(returncode=0)

        with mock.patch.object(reels.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(reels.subprocess, "run", side_effect=fake_run) as run:
            result = reels.render_reel(manifest, root, timeout=4)
        self.assertEqual(result["format"], "mp4")
        self.assertFalse(result["fallback"])
        self.assertEqual(Path(result["path"]), root / "reels" / "demo.mp4")
        self.assertTrue(Path(result["path"]).is_file())
        run.assert_called_once()

    def test_ffmpeg_absent_writes_scrollable_html_fallback(self):
        root, cover, _clip_a, _clip_b, moment = self._media()
        manifest = reels.build_reel_manifest(
            game={"game_id": "demo", "name": "Demo"},
            moments=[{"path": str(moment), "title": "A <moment>"}],
            cover=str(cover),
        )
        with mock.patch.object(reels.shutil, "which", return_value=None), \
             mock.patch.object(reels.subprocess, "run") as run:
            result = reels.render_reel(manifest, root)
        self.assertEqual(result["format"], "html")
        self.assertTrue(result["fallback"])
        self.assertEqual(Path(result["path"]), root / "reels" / "demo.html")
        html = Path(result["path"]).read_text()
        self.assertIn("<img", html)
        self.assertIn("A &lt;moment&gt;", html)
        self.assertIn("main", html)
        run.assert_not_called()

    def test_ffmpeg_failure_falls_back_to_html(self):
        root, cover, _clip_a, _clip_b, _moment = self._media()
        manifest = reels.build_reel_manifest(game_id="broken", cover=str(cover))
        error = subprocess.CalledProcessError(1, ["ffmpeg"])
        with mock.patch.object(reels.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(reels.subprocess, "run", side_effect=error):
            result = reels.render_reel(manifest, root)
        self.assertEqual(result["format"], "html")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["error"], "CalledProcessError")
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
