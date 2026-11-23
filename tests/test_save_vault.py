"""Unit tests for Save Vault snapshots, retention, diffing, and safety restores."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from api_errors import (  # noqa: E402
    VaultGameMismatch,
    VaultLimitExceeded,
    VaultNoSaves,
    VaultPathEscape,
    VaultSnapshotMissing,
)
from pkg.parity.parity_backup import BACKUP_ITEMS  # noqa: E402
from pkg.parity import parity_save_vault as sv  # noqa: E402


class SaveVaultUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True)
        self.save_dir = self.tmp / "saves" / "zelda"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "slot1.srm").write_bytes(b"ZELDA-SAVE-V1")
        (self.save_dir / "sub").mkdir()
        (self.save_dir / "sub" / "meta.bin").write_bytes(b"\x01\x02\x03")
        self.shared_file = self.tmp / "memcard" / "Mcd001.ps2"
        self.shared_file.parent.mkdir(parents=True)
        self.shared_file.write_bytes(b"SHARED-MEMCARD")
        self.game = {
            "id": 0,
            "game_id": "g-zelda",
            "name": "Zelda",
            "platform": "SNES",
            "save_paths": [str(self.save_dir), str(self.shared_file)],
            "save_sources": {str(self.shared_file): {"shared": True, "source": "pcsx2"}},
        }
        self.other_game = {
            "id": 1,
            "game_id": "g-metroid",
            "name": "Metroid",
            "platform": "SNES",
            "save_paths": [str(self.save_dir)],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_snapshot_crud_diff_restore_with_pre_restore_safety(self):
        t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        progress = []
        created = sv.create_snapshot(
            self.data_dir,
            self.game,
            trigger="manual",
            now=t0,
            progress_cb=lambda c, total: progress.append((c, total)),
        )
        snap_id = created["snapshot_id"]
        self.assertEqual(created["files"], 3)
        self.assertGreater(created["bytes"], 20)
        self.assertEqual(progress[-1], (3, 3))

        listed = sv.list_snapshots(self.data_dir, game_id="g-zelda")
        self.assertEqual([r["id"] for r in listed], [snap_id])

        # Modify live save, remove one file, add an extra file
        (self.save_dir / "slot1.srm").write_bytes(b"ZELDA-SAVE-V2-NEW")
        (self.save_dir / "sub" / "meta.bin").unlink()
        (self.save_dir / "extra.sav").write_bytes(b"EXTRA")

        diff = sv.diff_snapshot(self.data_dir, self.game, snap_id)
        self.assertEqual(diff["counts"]["added"], 1)
        self.assertEqual(diff["counts"]["changed"], 1)
        self.assertEqual(diff["counts"]["removed"], 1)
        self.assertEqual(diff["counts"]["unchanged"], 1)

        dry = sv.restore_snapshot(self.data_dir, self.game, snap_id, dry_run=True, now=t0)
        self.assertTrue(dry["dry_run"])
        self.assertIsNone(dry["safety_snapshot_id"])
        self.assertEqual((self.save_dir / "slot1.srm").read_bytes(), b"ZELDA-SAVE-V2-NEW")

        restored = sv.restore_snapshot(
            self.data_dir,
            self.game,
            snap_id,
            include_removals=True,
            now=t0 + timedelta(minutes=5),
        )
        self.assertIsNotNone(restored["safety_snapshot_id"])
        self.assertEqual((self.save_dir / "slot1.srm").read_bytes(), b"ZELDA-SAVE-V1")
        self.assertTrue((self.save_dir / "sub" / "meta.bin").is_file())
        self.assertFalse((self.save_dir / "extra.sav").exists())
        self.assertEqual(restored["counts"]["restored"], 2)
        self.assertEqual(restored["counts"]["removed"], 1)
        self.assertEqual(restored["counts"]["unchanged"], 1)

        deleted = sv.delete_snapshot(self.data_dir, self.game, snap_id)
        self.assertTrue(deleted["deleted"])
        with self.assertRaises(VaultSnapshotMissing):
            sv.diff_snapshot(self.data_dir, self.game, snap_id)

    def test_auto_trigger_skips_shared_roots_and_retention_exempts_pre_restore(self):
        t0 = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
        auto_snap = sv.create_snapshot(
            self.data_dir,
            self.game,
            trigger="auto",
            keep=2,
            now=t0,
        )
        # Shared memcard was excluded on auto trigger; only 2 files in self.save_dir
        self.assertEqual(auto_snap["files"], 2)

        # Create safety snapshot via restore, then push 3 more manual snapshots with keep=2
        (self.save_dir / "slot1.srm").write_bytes(b"MODIFIED")
        res = sv.restore_snapshot(self.data_dir, self.game, auto_snap["snapshot_id"], now=t0 + timedelta(minutes=1))
        safety_id = res["safety_snapshot_id"]

        ids = []
        for idx in range(3):
            snap = sv.create_snapshot(
                self.data_dir,
                self.game,
                trigger="manual",
                keep=2,
                now=t0 + timedelta(minutes=2 + idx),
            )
            ids.append(snap["snapshot_id"])

        remaining = [row["id"] for row in sv.list_snapshots(self.data_dir, game_id="g-zelda")]
        self.assertIn(safety_id, remaining)
        self.assertEqual(remaining.count(ids[-1]), 1)
        self.assertEqual(len([r for r in remaining if r != safety_id]), 2)

    def test_malformed_index_rebuilds_from_snapshot_manifests(self):
        created = sv.create_snapshot(self.data_dir, self.game, trigger="manual")
        index_path = sv.vault_dir_for(self.data_dir) / sv.INDEX_FILE_NAME
        index_path.write_text("{corrupt json", encoding="utf-8")

        rebuilt_list = sv.list_snapshots(self.data_dir, game_id="g-zelda")
        self.assertEqual(len(rebuilt_list), 1)
        self.assertEqual(rebuilt_list[0]["id"], created["snapshot_id"])
        summary = sv.vault_summary_readonly(self.data_dir)
        self.assertEqual(summary["snapshots"], 1)
        self.assertEqual(summary["games"], 1)

    def test_wrong_game_missing_saves_and_path_escapes(self):
        created = sv.create_snapshot(self.data_dir, self.game, trigger="manual")
        with self.assertRaises(VaultGameMismatch):
            sv.diff_snapshot(self.data_dir, self.other_game, created["snapshot_id"])

        with self.assertRaises(VaultNoSaves):
            sv.create_snapshot(self.data_dir, {"game_id": "g-none", "name": "None", "save_paths": []})

        for bad in ("", "../etc/passwd", "/etc/passwd", "a\\b", "C:foo", "dir/../x"):
            with self.assertRaises(VaultPathEscape):
                sv._validate_relpath(bad)

        evil_link = self.save_dir / "link.srm"
        evil_link.symlink_to(self.tmp / "outside.txt")
        with self.assertRaises(VaultPathEscape):
            sv.create_snapshot(self.data_dir, self.game, trigger="manual")

    def test_size_and_file_count_bounds_and_warning_flag(self):
        with mock.patch.object(sv, "MAX_SNAPSHOT_BYTES", 5):
            with self.assertRaises(VaultLimitExceeded):
                sv.create_snapshot(self.data_dir, self.game, trigger="manual")

        with mock.patch.object(sv, "MAX_SNAPSHOT_FILES", 2):
            with self.assertRaises(VaultLimitExceeded):
                sv.create_snapshot(self.data_dir, self.game, trigger="manual")

        sv.create_snapshot(self.data_dir, self.game, trigger="manual")
        with mock.patch.object(sv, "SIZE_WARNING_BYTES", 10):
            st = sv.vault_status(self.data_dir, game_id="g-zelda", settings={"save_vault_keep": 12})
        self.assertTrue(st["over_5gb_warning"])
        self.assertEqual(st["settings"]["save_vault_keep"], 12)
        self.assertNotIn(sv.VAULT_DIR_NAME, BACKUP_ITEMS)

    def test_auto_vault_schedule_and_pass(self):
        t0 = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
        state = {
            "settings": {"save_management_enabled": True, "save_vault_enabled": True, "save_vault_auto_days": 7, "save_vault_keep": 3},
            "games": [self.game, {"game_id": "g-empty", "name": "Empty", "save_paths": []}],
        }
        self.assertTrue(sv.auto_vault_due(self.data_dir, state["settings"], now=t0))
        res = sv.run_auto_vault_pass(self.data_dir, state, now=t0)
        self.assertEqual(res["snapshotted"], 1)
        self.assertEqual(res["skipped"], 1)
        self.assertFalse(sv.auto_vault_due(self.data_dir, state["settings"], now=t0 + timedelta(days=2)))
        self.assertTrue(sv.auto_vault_due(self.data_dir, state["settings"], now=t0 + timedelta(days=8)))

    def test_file_root_readonly_summary_escapes_and_error_branches(self):
        # game_vault_key fallback without game_id
        self.assertTrue(sv.game_vault_key({"name": "Untagged", "platform": "GB"}).startswith("g-"))
        self.assertEqual(sv.vault_summary_readonly(self.data_dir), {"snapshots": 0, "bytes": 0, "games": 0})
        idx_file = sv.vault_dir_for(self.data_dir) / sv.INDEX_FILE_NAME
        idx_file.parent.mkdir(parents=True, exist_ok=True)
        idx_file.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(sv.vault_summary_readonly(self.data_dir), {"snapshots": 0, "bytes": 0, "games": 0})
        idx_file.write_text('{"snapshots":"bad"}', encoding="utf-8")
        self.assertEqual(sv.vault_summary_readonly(self.data_dir), {"snapshots": 0, "bytes": 0, "games": 0})

        # Single-file save root create/diff/restore
        single = self.tmp / "single" / "slot.srm"
        single.parent.mkdir(parents=True)
        single.write_bytes(b"ONE-FILE-V1")
        f_game = {"game_id": "g-single", "name": "Single", "save_paths": [str(single)]}
        snap = sv.create_snapshot(self.data_dir, f_game, trigger="invalid-trigger")
        single.write_bytes(b"ONE-FILE-V2")
        diff = sv.diff_snapshot(self.data_dir, f_game, snap["snapshot_id"])
        self.assertEqual(diff["counts"]["changed"], 1)
        sv.restore_snapshot(self.data_dir, f_game, snap["snapshot_id"], skip_safety=True)
        self.assertEqual(single.read_bytes(), b"ONE-FILE-V1")

        # Containment escape checks
        with self.assertRaises(VaultPathEscape):
            sv._resolve_contained_target(single, "other.srm", is_file_root=True)
        with self.assertRaises(VaultPathEscape):
            sv._safe_snapshot_dir(sv.vault_dir_for(self.data_dir), "k", "..")

        # auto_vault_due guards & run_auto_vault_pass cancel
        self.assertFalse(sv.auto_vault_due(self.data_dir, {"save_management_enabled": False}))
        self.assertFalse(sv.auto_vault_due(self.data_dir, {"save_vault_auto_days": "bad"}))
        self.assertFalse(sv.auto_vault_due(self.data_dir, {"save_vault_auto_days": 0}))
        ev = sv.VaultPathEscape
        _ = ev
        cancel = mock.Mock(is_set=lambda: True)
        c_res = sv.run_auto_vault_pass(self.data_dir, {"games": [f_game, {"hidden": True}]}, cancel_event=cancel)
        self.assertEqual(c_res["snapshotted"], 0)

        # Extra live file triggers removed diff bucket + include_removals=True cleanup + naive now
        naive_t = datetime(2026, 10, 12, 8, 0)
        dir_snap = sv.create_snapshot(self.data_dir, self.game, trigger="manual", now=naive_t)
        extra_live = self.save_dir / "slot2.srm"
        extra_live.write_bytes(b"EXTRA")
        diff_rem = sv.diff_snapshot(self.data_dir, self.game, dir_snap["snapshot_id"])
        self.assertEqual(diff_rem["counts"]["removed"], 1)
        restored = sv.restore_snapshot(self.data_dir, self.game, dir_snap["snapshot_id"], skip_safety=True, include_removals=True, now=naive_t)
        self.assertEqual(restored["counts"]["removed"], 1)
        self.assertFalse(extra_live.exists())

        # rebuild_index skips bogus files/dirs and keeps valid manifest
        vdir = sv.vault_dir_for(self.data_dir) / sv.game_vault_key(self.game)
        (vdir / "empty_folder").mkdir(exist_ok=True)
        (vdir / "corrupt_snap").mkdir(exist_ok=True)
        (vdir / "corrupt_snap" / sv.SNAPSHOT_FILE_NAME).write_text("{broken", encoding="utf-8")
        (vdir / "nodict_snap").mkdir(exist_ok=True)
        (vdir / "nodict_snap" / sv.SNAPSHOT_FILE_NAME).write_text("{}", encoding="utf-8")
        rebuilt = sv.rebuild_index(self.data_dir)
        self.assertGreaterEqual(len(rebuilt["snapshots"]), 1)

        # Bad game object -> VaultNoSaves; bad keep clamp; bad auto timestamp -> due True
        with self.assertRaises(VaultNoSaves):
            sv.resolve_game_save_roots("not-a-dict")  # type: ignore[arg-type]
        self.assertEqual(sv._clamp_keep("bad"), sv.DEFAULT_KEEP)
        self.assertEqual(sv._clamp_keep(999), sv.MAX_KEEP)

        idx = sv.load_index(self.data_dir)
        idx["last_auto_at"] = "invalid-ts"
        sv._write_index_locked(sv.vault_dir_for(self.data_dir), idx)
        self.assertTrue(sv.auto_vault_due(self.data_dir, {"save_vault_auto_days": 7}, now=naive_t))

        # Symlink inside live dir raises VaultPathEscape in diff_snapshot
        live_link = self.save_dir / "bad_link.srm"
        live_link.symlink_to(self.tmp / "outside.txt")
        with self.assertRaises(VaultPathEscape):
            sv.diff_snapshot(self.data_dir, self.game, dir_snap["snapshot_id"])
        live_link.unlink()

        # Symlink inside intermediate restore folder raises VaultPathEscape
        sub = self.save_dir / "sub2"
        sub.mkdir()
        (sub / "inner.srm").write_bytes(b"IN")
        sub_snap = sv.create_snapshot(self.data_dir, self.game, trigger="manual")
        shutil.rmtree(sub)
        sub.symlink_to(self.tmp)
        with self.assertRaises(VaultPathEscape):
            sv.restore_snapshot(self.data_dir, self.game, sub_snap["snapshot_id"], skip_safety=True)
        sub.unlink()

        # Empty existing dir -> VaultNoSaves
        empty_dir = self.tmp / "empty_saves"
        empty_dir.mkdir()
        with self.assertRaises(VaultNoSaves):
            sv.create_snapshot(self.data_dir, {"game_id": "g-em", "name": "Em", "save_paths": [str(empty_dir)]})


if __name__ == "__main__":
    unittest.main()
