#!/usr/bin/env python3
"""P2-14: operation checkpoints accept append-only deltas without re-copying."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.state.operations as operations_module  # noqa: E402
from pkg.state.operations import OperationService  # noqa: E402


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = OperationService(Path(self.tempdir.name) / "library.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def _running_operation(self, checkpoint=None):
        job = self.service.create(
            operation_type="media.bulk_download",
            title="Bulk media",
            checkpoint=checkpoint,
        )
        self.service.mark_running(job["job_id"])
        return job["job_id"]

    def test_checkpoint_append_merges_lists(self):
        job_id = self._running_operation({"failed_game_ids": ["a"]})
        self.service.update_progress(
            job_id,
            checkpoint_append={"failed_game_ids": ["b"], "completed_game_ids": ["c"]},
        )
        checkpoint = self.service.get(job_id)["checkpoint"]
        self.assertEqual(checkpoint["failed_game_ids"], ["a", "b"])
        self.assertEqual(checkpoint["completed_game_ids"], ["c"])

    def test_repeated_identical_checkpoint_is_detached_once(self):
        job_id = self._running_operation({"failed_game_ids": ["a"]})
        checkpoint = {"failed_game_ids": ["b"]}
        with mock.patch.object(
            operations_module.copy, "deepcopy", wraps=operations_module.copy.deepcopy
        ) as spy:
            self.service.update_progress(job_id, checkpoint=checkpoint)
            calls_after_first = spy.call_count
            self.service.update_progress(job_id, checkpoint=checkpoint)
            calls_after_second = spy.call_count
        self.assertLessEqual(calls_after_first, 1)
        self.assertEqual(calls_after_second, calls_after_first, "unchanged checkpoint must be reused")
        stored = self.service.get(job_id)["checkpoint"]
        self.assertEqual(stored["failed_game_ids"], ["b"])
        self.assertNotIn("_checkpoint_source", stored)

    def test_public_documents_hide_internal_markers(self):
        job_id = self._running_operation({"failed_game_ids": []})
        document = self.service.get(job_id)
        self.assertTrue(all(not key.startswith("_") for key in document))
        self.assertEqual(self.service.list_jobs()["jobs"][0]["job_id"], job_id)


if __name__ == "__main__":
    unittest.main()
