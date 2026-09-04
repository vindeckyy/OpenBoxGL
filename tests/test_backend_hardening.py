#!/usr/bin/env python3

import hashlib
import io
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from backend_io import download_file, read_limited
from catalog import bulk_update
from parity_gameyfin import GameyfinError, uninstall_gameyfin_game
from state_store import JsonStateStore, StateCorruptError, STATE_SCHEMA_VERSION


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(payload)
        self.headers = headers or {"Content-Type": "application/octet-stream", "Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class BackendHardeningTests(unittest.TestCase):
    def test_state_migration_ids_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save({"games": [{"name": "Legacy", "path": "/tmp/legacy.rom"}], "profiles": {}, "history": []})
            state = store.load()
            self.assertEqual(state["schema_version"], STATE_SCHEMA_VERSION)
            stable_id = state["games"][0]["game_id"]
            self.assertTrue(stable_id)
            store.save({**state, "games": [{**state["games"][0], "favorite": True}]})
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                store.load()
            recovered = store.recover()
            self.assertEqual(recovered["games"][0]["game_id"], stable_id)
            self.assertTrue(recovered["games"][0]["favorite"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_stable_ids_are_accepted_by_bulk_updates(self):
        games = [{"game_id": "game-a", "name": "A"}, {"game_id": "game-b", "name": "B"}]
        self.assertEqual(bulk_update(games, ["game-b"], {"favorite": True}), 1)
        self.assertTrue(games[1]["favorite"])

    def test_bounded_atomic_download_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "payload.bin"
            payload = b"safe payload"
            digest = hashlib.sha256(payload).hexdigest()
            result = download_file(
                "https://example.invalid/payload",
                destination,
                opener=lambda *_args, **_kwargs: FakeResponse(payload),
                sha256=digest,
                max_bytes=1024,
            )
            self.assertEqual(result.read_bytes(), payload)
            with self.assertRaises(ValueError):
                download_file(
                    "https://example.invalid/payload",
                    destination,
                    opener=lambda *_args, **_kwargs: FakeResponse(payload),
                    sha256="0" * 64,
                    max_bytes=1024,
                )

    def test_gameyfin_uninstall_rejects_outside_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install"
            root.mkdir()
            outside = Path(directory) / "outside.dat"
            outside.write_text("keep", encoding="utf-8")
            with self.assertRaises(GameyfinError):
                uninstall_gameyfin_game({"install_dir": str(root), "path": str(outside)}, root)
            self.assertTrue(outside.exists())

    def test_read_limited_rejects_negative_and_huge_content_length(self):
        # Negative Content-Length must be rejected up front.
        with self.assertRaises(ValueError):
            read_limited(FakeResponse(b"x", headers={"Content-Length": "-1"}), max_bytes=8)
        # A huge declared length must not produce a negative read size.
        with self.assertRaises(ValueError):
            read_limited(FakeResponse(b"x", headers={"Content-Length": "999999999999"}), max_bytes=8)
        # Oversize body beyond the limit raises.
        with self.assertRaises(ValueError):
            read_limited(FakeResponse(b"123456789"), max_bytes=8)

    def test_state_backup_matches_primary_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            store = JsonStateStore(path)
            store.save({"games": [{"name": "First"}], "profiles": {}, "history": []})
            store.save({"games": [{"name": "Second"}], "profiles": {}, "history": []})
            backup = json.loads(store.backup_path.read_text())
            # The backup mirrors the latest committed primary; snapshots
            # hold earlier states. It must never contain uncommitted bytes.
            self.assertEqual(backup["games"][0]["name"], "Second")
            primary = json.loads(path.read_text())
            self.assertEqual(primary["games"][0]["name"], "Second")

    def test_concurrent_update_writers_keep_both_changes(self):
        # Two store instances (native UI and web backend) committing via
        # update() under the cross-process flock must not lose each other's data.
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.json"
            seed = JsonStateStore(path)
            seed.save({"schema_version": 3, "games": [], "profiles": {},
                       "history": [], "settings": {}, "playlists": []})
            web_store = JsonStateStore(path)
            native_store = JsonStateStore(path)
            barrier = threading.Barrier(2)

            def web_add():
                barrier.wait()
                web_store.update(lambda state: state["games"].append({
                    "game_id": "game-web", "path": "/bin/true", "name": "WebGame",
                }))

            def native_touch():
                barrier.wait()
                native_store.update(lambda state: state["settings"].update({"native_touched": True}))

            threads = [threading.Thread(target=web_add), threading.Thread(target=native_touch)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            final = json.loads(path.read_text())
            self.assertEqual([game["game_id"] for game in final["games"]], ["game-web"])
            self.assertTrue(final["settings"]["native_touched"])

    def test_archive_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "link.zip"
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(info, "/etc/passwd")
            from archives import safe_zip_extract

            with self.assertRaises(ValueError):
                safe_zip_extract(archive, Path(directory) / "out")


if __name__ == "__main__":
    unittest.main()
