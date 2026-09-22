#!/usr/bin/env python3
"""F5: Artwork Doctor HTTP routes (report, fix, undo)."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers import steamgrid  # noqa: E402
from pkg.parity.parity_artwork_hygiene import write_undo_manifest  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def authorized(self):
        return True

    def handle_unauthorized(self):
        self.responses.append((403, {"error": "unauthorized"}))

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class ArtworkHygieneRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.cache = self.root / "cache"
        self.state = {"games": [], "settings": {"steamgrid_enabled": True}}

    def _png(self, name, width=600, height=900):
        import struct
        import zlib

        path = self.root / name
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
        chunks = b""
        for kind, payload in ((b"IHDR", ihdr), (b"IDAT", zlib.compress(raw)), (b"IEND", b"")):
            chunks += struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunks)
        return path

    def test_report_route_lists_issues_and_batches(self):
        cover = self._png("cover.png", 64, 64)
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": str(cover)}, {"game_id": "g2", "name": "Two"}]
        handler = Handler()
        with mock.patch.object(steamgrid.openbox, "load_state", return_value=self.state), mock.patch.object(
            steamgrid, "_cache_dir", return_value=self.cache
        ), mock.patch.object(steamgrid, "_settings", return_value=self.state["settings"]):
            steamgrid.steamgrid_hygiene_report(handler, SimpleNamespace(query=""))
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["scanned"], 2)
        self.assertEqual(payload["counts"]["missing_cover"], 1)
        self.assertGreaterEqual(payload["counts"]["low_res"], 1)
        self.assertEqual(payload["provider_attribution"], "SteamGridDB")
        self.assertEqual(payload["batches"], [])

    def test_hygiene_replace_one_applies_and_journals(self):
        current = self._png("current.png", 64, 64)
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": str(current)}]
        records = []
        entry = {"game_id": "g1", "name": "One", "field": "cover"}
        downloaded = []
        with mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache), mock.patch.object(
            steamgrid, "_media_root", return_value=self.root / "media"
        ), mock.patch.object(steamgrid, "search_games", return_value=[{"id": 7, "name": "One"}]) as search, mock.patch.object(
            steamgrid, "game_assets", return_value=[{"url": "https://cdn.example/cover.png", "width": 600, "height": 900}]
        ), mock.patch.object(
            steamgrid, "choose_media", return_value={"cover": "https://cdn.example/cover.png"}
        ), mock.patch.object(
            steamgrid, "download_bytes", side_effect=lambda url, dest: (downloaded.append(url), str(dest))[1]
        ), mock.patch.object(
            steamgrid, "bump_media_epoch"
        ), mock.patch.object(
            steamgrid, "transact_state", side_effect=lambda mutate: (self.state, mutate(self.state))
        ):
            steamgrid._hygiene_replace_one(self.state["games"][0], {**entry}, "batch-1", records, entry)
        search.assert_called_once()
        self.assertEqual(entry["status"], "applied")
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["new"].endswith("cover.png"))
        self.assertFalse(records[0]["created"])
        self.assertTrue(Path(records[0]["backup"]).is_file())
        self.assertEqual(self.state["games"][0]["artwork_provider"], "SteamGridDB")

    def test_undo_route_restores_fields_and_files(self):
        original = self._png("original.png", 600, 900)
        backup_dir = self.cache / "artwork-hygiene" / "batch-9"
        backup_dir.mkdir(parents=True)
        backup = backup_dir / "g1-cover.png"
        backup.write_bytes(original.read_bytes())
        manifest_record = {
            "game_id": "g1", "field": "cover", "previous": str(original),
            "created": False, "backup": str(backup), "new": str(original),
        }
        write_undo_manifest(self.cache, "batch-9", [manifest_record])
        original.write_bytes(b"replaced")
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": "replaced"}]
        handler = Handler()
        with mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache), mock.patch.object(
            steamgrid, "transact_state", side_effect=lambda mutate: (self.state, mutate(self.state))
        ), mock.patch.object(steamgrid, "bump_media_epoch"):
            steamgrid.steamgrid_hygiene_undo(handler, {"batch_id": "batch-9"})
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["restored"], 1)
        self.assertEqual(original.read_bytes(), backup.read_bytes())
        self.assertEqual(self.state["games"][0]["cover"], str(original))

    def test_undo_route_rejects_unknown_batch(self):
        from api_errors import BadRequest

        with mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache):
            with self.assertRaises(BadRequest) as raised:
                steamgrid.steamgrid_hygiene_undo(Handler(), {"batch_id": "missing"})
        self.assertEqual(raised.exception.code, "ARTWORK_UNDO_MISSING")
        with self.assertRaises(BadRequest):
            steamgrid.steamgrid_hygiene_undo(Handler(), {})

    def test_fix_route_requires_provider_and_queues_job(self):
        from api_errors import BadRequest

        with mock.patch.object(steamgrid, "_settings", return_value={"steamgrid_enabled": False}), mock.patch.object(
            steamgrid, "steamgrid_enabled", return_value=False
        ), mock.patch.object(steamgrid, "is_configured", return_value=False):
            with self.assertRaises(BadRequest) as raised:
                steamgrid.steamgrid_hygiene_fix(Handler(), {})
        self.assertEqual(raised.exception.code, "STEAMGRID_DISABLED")

        fake_job = {"job_id": "job-1"}
        with mock.patch.object(steamgrid, "_settings", return_value={"steamgrid_enabled": True}), mock.patch.object(
            steamgrid, "provider_available", return_value=True
        ), mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache), mock.patch.object(
            steamgrid.openbox, "load_state", return_value=self.state
        ), mock.patch.object(steamgrid.JOB_MANAGER, "submit", return_value=fake_job) as submit:
            handler = Handler()
            steamgrid.steamgrid_hygiene_fix(handler, {"fields": ["cover"], "limit": "10"})
        self.assertEqual(handler.responses[-1][1]["job_id"], "job-1")
        submit.assert_called_once()
        self.assertEqual(submit.call_args[0][0], "steamgrid-hygiene-fix")


if __name__ == "__main__":
    unittest.main()
