"""Fault-injection tests for Save Vault mid-copy crashes and restore integrity."""

from __future__ import annotations

import errno
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import VaultSafetySnapshotFailed  # noqa: E402
from pkg.parity import parity_save_vault as sv  # noqa: E402


class SaveVaultFaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True)
        self.save_dir = self.tmp / "saves"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "a.srm").write_bytes(b"ALPHA")
        (self.save_dir / "b.srm").write_bytes(b"BETA")
        self.game = {"game_id": "g-fault", "name": "FaultGame", "platform": "GBA", "save_paths": [str(self.save_dir)]}

    def tearDown(self):
        self._tmp.cleanup()

    def test_mid_copy_disk_full_cleans_staging_and_leaves_index_intact(self):
        real_copy = sv._copy_with_sha256
        calls = {"n": 0}

        def flaky_copy(src, dest):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_copy(src, dest)

        with mock.patch.object(sv, "_copy_with_sha256", side_effect=flaky_copy):
            with self.assertRaises(OSError):
                sv.create_snapshot(self.data_dir, self.game, trigger="manual")

        self.assertEqual(sv.list_snapshots(self.data_dir), [])
        game_dir = sv.vault_dir_for(self.data_dir) / sv.game_vault_key(self.game)
        leftovers = list(game_dir.glob(".staging-*")) if game_dir.exists() else []
        self.assertEqual(leftovers, [])

    def test_failed_safety_snapshot_aborts_restore_before_mutating_live_saves(self):
        created = sv.create_snapshot(self.data_dir, self.game, trigger="manual")
        snap_id = created["snapshot_id"]
        (self.save_dir / "a.srm").write_bytes(b"LIVE-PROGRESS")

        with mock.patch.object(sv, "create_snapshot", side_effect=OSError("disk error")):
            with self.assertRaises(VaultSafetySnapshotFailed) as raised:
                sv.restore_snapshot(self.data_dir, self.game, snap_id)
        self.assertEqual(raised.exception.code, "VAULT_SAFETY_SNAPSHOT_FAILED")
        self.assertEqual((self.save_dir / "a.srm").read_bytes(), b"LIVE-PROGRESS")

    def test_restore_detects_missing_and_corrupted_snapshot_payloads(self):
        created = sv.create_snapshot(self.data_dir, self.game, trigger="manual")
        snap_id = created["snapshot_id"]
        index = sv.load_index(self.data_dir)
        entry = index["snapshots"][0]
        snap_dir = sv.vault_dir_for(self.data_dir) / entry["game_key"] / entry["folder"]

        # Corrupt one archived file and delete the other archived file
        manifest = json.loads((snap_dir / sv.SNAPSHOT_FILE_NAME).read_text(encoding="utf-8"))
        first_payload = snap_dir / manifest["files"][0]["archive_rel"]
        second_payload = snap_dir / manifest["files"][1]["archive_rel"]
        first_payload.write_bytes(b"CORRUPTED")
        second_payload.unlink()

        (self.save_dir / "a.srm").write_bytes(b"ORIGINAL-A")
        (self.save_dir / "b.srm").write_bytes(b"ORIGINAL-B")

        res = sv.restore_snapshot(self.data_dir, self.game, snap_id)
        self.assertEqual(res["counts"]["error"], 2)
        self.assertEqual(res["counts"]["restored"], 0)
        self.assertEqual((self.save_dir / "a.srm").read_bytes(), b"ORIGINAL-A")
        self.assertEqual((self.save_dir / "b.srm").read_bytes(), b"ORIGINAL-B")


if __name__ == "__main__":
    unittest.main()
