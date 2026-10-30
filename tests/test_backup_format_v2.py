"""Backup format 2 (ADR 0093): manifest hashes, refusal before any write, v1 reads, 1.19-compatible export."""

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_errors import BackupFormatUnsupported  # noqa: E402
from pkg.parity import parity_backup as backup  # noqa: E402
from state_store import STATE_SCHEMA_VERSION, default_state  # noqa: E402


def _state():
    state = default_state()
    state["games"] = [{"game_id": "game-a", "name": "A", "path": "/roms/a.iso"}]
    state["settings"] = {"theme": "dark"}
    return state


def _read_archive(archive):
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("manifest.json"))
        members = {name: package.read(name) for name in package.namelist() if name != "manifest.json"}
    return manifest, members


def _write_archive(path, manifest, members):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            package.writestr(name, data)
    return path


class BackupFormatV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "data"
        self.root.mkdir()
        (self.root / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_v2_manifest_lists_every_member_with_its_size_and_hash(self):
        archive = backup.create_backup(self.root, _state(), ["library", "settings"])
        manifest, members = _read_archive(archive)
        self.assertEqual(manifest["format"], 2)
        self.assertEqual(manifest["state_schema"], STATE_SCHEMA_VERSION)
        self.assertEqual(manifest["encryption"], "none")
        listed = {entry["name"]: entry for entry in manifest["files"]}
        self.assertEqual(set(listed), set(members))
        for name, data in members.items():
            self.assertEqual(listed[name]["size"], len(data))
            self.assertEqual(listed[name]["sha256"], hashlib.sha256(data).hexdigest())

    def test_v2_round_trip_restores_the_library_into_a_new_folder(self):
        archive = backup.create_backup(self.root, _state(), ["library", "settings"])
        target = self.base / "restored"
        restored_items = backup.restore_backup(archive, target, force=True)["items"]
        self.assertIn("library", restored_items)
        restored = json.loads((target / "library.json").read_text(encoding="utf-8"))
        self.assertEqual([game["game_id"] for game in restored["games"]], ["game-a"])

    def test_restore_reports_each_member_with_its_hash_check_and_outcome(self):
        archive = backup.create_backup(self.root, _state(), ["library", "settings"])
        target = self.base / "selective"
        result = backup.restore_backup(archive, target, items=["settings"], force=True)
        rows = {row["name"]: row for row in result["files"]}
        self.assertEqual(result["items"], ["settings"])
        self.assertTrue(rows["library.json"]["verified"])
        self.assertEqual(rows["library.json"]["status"], "skipped")
        self.assertEqual(rows["settings.json"]["status"], "restored")
        self.assertEqual(rows["manifest.json"]["status"], "manifest")
        self.assertFalse((target / "library.json").exists())

    def test_tampered_member_is_refused_before_anything_is_written(self):
        archive = backup.create_backup(self.root, _state(), ["library", "settings"])
        manifest, members = _read_archive(archive)
        members["settings.json"] = b'{"theme": "tampered"}'
        tampered = _write_archive(self.base / "tampered.zip", manifest, members)
        target = self.base / "target"
        target.mkdir()
        sentinel = target / "settings.json"
        sentinel.write_text('{"theme": "keep"}', encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            backup.restore_backup(tampered, target, items=["settings"], force=True)
        self.assertIn("SHA-256", str(raised.exception))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"theme": "keep"}')

    def test_archive_member_the_manifest_does_not_list_is_refused(self):
        archive = backup.create_backup(self.root, _state(), ["settings"])
        manifest, members = _read_archive(archive)
        members["extra.bin"] = b"unlisted"
        smuggled = _write_archive(self.base / "smuggled.zip", manifest, members)
        with self.assertRaises(ValueError) as raised:
            backup.restore_backup(smuggled, self.base / "target", force=True)
        self.assertIn("does not list", str(raised.exception))

    def test_a_format_newer_than_this_build_is_refused_with_its_code(self):
        future = _write_archive(
            self.base / "future.zip",
            {"format": 3, "items": ["settings"]},
            {"settings.json": b"{}"},
        )
        target = self.base / "target"
        with self.assertRaises(BackupFormatUnsupported) as raised:
            backup.restore_backup(future, target, force=True)
        self.assertEqual(raised.exception.code, "BACKUP_FORMAT_UNSUPPORTED")
        self.assertFalse((target / "settings.json").exists())

    def test_a_format_one_archive_without_hashes_still_restores(self):
        v1 = _write_archive(self.base / "v1.zip", {"items": ["settings"]}, {"settings.json": b'{"theme": "legacy"}'})
        target = self.base / "target"
        backup.restore_backup(v1, target, force=True)
        self.assertEqual(json.loads((target / "settings.json").read_text(encoding="utf-8")), {"theme": "legacy"})

    def test_one_point_nine_compatible_export_writes_the_1_19_layout(self):
        archive = backup.create_backup(self.root, _state(), ["library", "settings"], format_version=1)
        manifest, members = _read_archive(archive)
        self.assertNotIn("format", manifest)
        self.assertNotIn("files", manifest)
        library = json.loads(members["library.json"])
        self.assertEqual(library["schema_version"], 6)
        for key in ("format", "library_id", "path_mode", "library_root", "created_with"):
            self.assertNotIn(key, library)
        self.assertEqual(library["games"][0]["game_id"], "game-a")

    def test_unknown_export_format_is_refused(self):
        with self.assertRaises(BackupFormatUnsupported):
            backup.create_backup(self.root, _state(), ["settings"], format_version=9)


if __name__ == "__main__":
    unittest.main()
