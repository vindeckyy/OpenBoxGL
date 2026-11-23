"""HTTP handler tests for Save Vault routes, problem codes, and job lifecycle."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from api_errors import (  # noqa: E402
    BadRequest,
    GameNotFound,
    VaultGameMismatch,
    VaultNoSaves,
    VaultSnapshotMissing,
)
from handlers import savevault  # noqa: E402
import job_manager  # noqa: E402


class MockHandler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class SaveVaultHttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_file = self.tmp / "data" / "state.json"
        self.data_file.parent.mkdir(parents=True)
        self.save_dir = self.tmp / "saves"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "save.dat").write_bytes(b"SAVE-STATE-1")
        self.state = {
            "games": [
                {"id": 0, "game_id": "g-1", "name": "Chrono", "platform": "SNES", "save_paths": [str(self.save_dir)]},
                {"id": 1, "game_id": "g-2", "name": "Empty", "platform": "SNES", "save_paths": []},
            ],
            "settings": {"save_management_enabled": True, "save_vault_enabled": True, "save_vault_keep": 5},
        }
        self.jm = job_manager.JobManager(max_workers=1)

    def tearDown(self):
        self.jm.shutdown(wait=True)
        self._tmp.cleanup()

    def _wait_job(self, name: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.jm.snapshot(name)
            if snap.get("state") in {"done", "error", "cancelled"}:
                return snap
            time.sleep(0.01)
        self.fail(f"Timed out waiting for job {name}")

    def _run_patches(self):
        return (
            mock.patch.object(savevault, "DATA", self.data_file),
            mock.patch.object(savevault, "load_state_readonly", return_value=self.state),
            mock.patch.object(savevault, "JOB_MANAGER", self.jm),
        )

    def test_snapshot_diff_restore_and_delete_routes(self):
        p1, p2, p3 = self._run_patches()
        with p1, p2, p3:
            h_snap = MockHandler()
            savevault.vault_snapshot(h_snap, {"game_id": "g-1", "trigger": "manual"})
            self.assertEqual(h_snap.responses[0][0], 202)
            job = self._wait_job("save-vault-snapshot:g-1")
            self.assertEqual(job["state"], "done")
            snap_id = job["snapshot_id"]

            h_list = MockHandler()
            savevault.vault_list(h_list, SimpleNamespace(query="game_id=g-1"))
            self.assertEqual(h_list.responses[0][0], 200)
            self.assertEqual(len(h_list.responses[0][1]["snapshots"]), 1)

            h_stat = MockHandler()
            savevault.vault_get_status(h_stat, SimpleNamespace(query="game_id=g-1"))
            self.assertTrue(h_stat.responses[0][1]["enabled"])
            self.assertEqual(h_stat.responses[0][1]["game_snapshots"], 1)

            (self.save_dir / "save.dat").write_bytes(b"SAVE-STATE-2")
            h_diff = MockHandler()
            savevault.vault_diff(h_diff, SimpleNamespace(query=f"game_id=g-1&snapshot_id={snap_id}"))
            self.assertEqual(h_diff.responses[0][1]["counts"]["changed"], 1)

            h_dry = MockHandler()
            savevault.vault_restore(h_dry, {"game_id": "g-1", "snapshot_id": snap_id, "dry_run": True})
            self.assertEqual(h_dry.responses[0][0], 200)
            self.assertTrue(h_dry.responses[0][1]["dry_run"])

            h_restore = MockHandler()
            savevault.vault_restore(h_restore, {"game_id": "g-1", "snapshot_id": snap_id, "dry_run": False})
            self.assertEqual(h_restore.responses[0][0], 202)
            r_job = self._wait_job("save-vault-restore:g-1")
            self.assertEqual(r_job["state"], "done")
            self.assertEqual((self.save_dir / "save.dat").read_bytes(), b"SAVE-STATE-1")

            h_del = MockHandler()
            savevault.vault_delete(h_del, {"game_id": "g-1", "snapshot_id": snap_id})
            self.assertTrue(h_del.responses[0][1]["deleted"])

    def test_error_codes_and_disabled_settings_gates(self):
        p1, p2, p3 = self._run_patches()
        with p1, p2, p3:
            with self.assertRaises(VaultNoSaves) as err_ns:
                savevault.vault_snapshot(MockHandler(), {"game_id": "g-2"})
            self.assertEqual(err_ns.exception.code, "NO_SAVES")

            with self.assertRaises(GameNotFound):
                savevault.vault_list(MockHandler(), SimpleNamespace(query="game_id=missing"))

            with self.assertRaises(VaultSnapshotMissing) as err_miss:
                savevault.vault_diff(MockHandler(), SimpleNamespace(query="game_id=g-1&snapshot_id=snap-nope"))
            self.assertEqual(err_miss.exception.code, "VAULT_SNAPSHOT_MISSING")

            # Disable automatic vault snapshots while keeping manual allowed
            self.state["settings"]["save_vault_enabled"] = False
            with self.assertRaises(BadRequest):
                savevault.vault_snapshot(MockHandler(), {"game_id": "g-1", "trigger": "auto"})

            # Turn save management completely off
            self.state["settings"]["save_management_enabled"] = False
            h_list = MockHandler()
            savevault.vault_list(h_list, SimpleNamespace(query=""))
            self.assertFalse(h_list.responses[0][1]["enabled"])
            h_stat = MockHandler()
            savevault.vault_get_status(h_stat, SimpleNamespace(query=""))
            self.assertFalse(h_stat.responses[0][1]["enabled"])
            with self.assertRaises(BadRequest):
                savevault.vault_snapshot(MockHandler(), {"game_id": "g-1", "trigger": "manual"})

            self.state["settings"]["save_management_enabled"] = True
            with self.assertRaises(VaultGameMismatch) as err_mismatch:
                h_snap = MockHandler()
                savevault.vault_snapshot(h_snap, {"game_id": "g-1"})
                job = self._wait_job("save-vault-snapshot:g-1")
                snap_id = job["snapshot_id"]
                savevault.vault_delete(MockHandler(), {"game_id": "g-2", "snapshot_id": snap_id})
            self.assertEqual(err_mismatch.exception.code, "VAULT_GAME_MISMATCH")

            # Input validation branches across handlers
            with self.assertRaises(BadRequest):
                savevault.vault_snapshot(MockHandler(), "bad")  # type: ignore[arg-type]
            with self.assertRaises(BadRequest):
                savevault.vault_snapshot(MockHandler(), {"game_id": "g-1", "trigger": "bogus"})
            with self.assertRaises(BadRequest):
                savevault.vault_diff(MockHandler(), SimpleNamespace(query="game_id=g-1"))
            with self.assertRaises(BadRequest):
                savevault.vault_restore(MockHandler(), "bad")  # type: ignore[arg-type]
            with self.assertRaises(BadRequest):
                savevault.vault_restore(MockHandler(), {"game_id": "g-1"})
            with self.assertRaises(BadRequest):
                savevault.vault_delete(MockHandler(), "bad")  # type: ignore[arg-type]
            with self.assertRaises(BadRequest):
                savevault.vault_delete(MockHandler(), {})


if __name__ == "__main__":
    unittest.main()
