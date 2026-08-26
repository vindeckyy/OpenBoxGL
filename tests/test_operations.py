#!/usr/bin/env python3
"""Tests for the durable operation service (F14)."""

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.state.operations import (
    OPERATION_DOCUMENT_KEYS,
    SSE_EVENT_CANCELLING,
    SSE_EVENT_FINISHED,
    SSE_EVENT_INTERRUPTED,
    SSE_EVENT_PROGRESS,
    SSE_EVENT_QUEUED,
    SSE_PAYLOAD_KEYS,
    JobNotCancellableError,
    JobNotResumableError,
    JobStateConflictError,
    OperationService,
    operations_path,
)


class OperationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tempdir.name) / "library.json"
        self.data_path.write_text("{}", encoding="utf-8")
        self.service = OperationService(self.data_path)
        self.events: list[tuple[str, dict]] = []
        self.service.set_event_observer(lambda kind, payload: self.events.append((kind, payload)))

    def tearDown(self):
        self.tempdir.cleanup()

    def _create(self, operation_type="setup.scan", title="Test op", **kwargs):
        return self.service.create(operation_type=operation_type, title=title, **kwargs)

    def test_operation_document_keys_on_create(self):
        op = self._create(input_data={"path": "/home/user/games"})
        for key in OPERATION_DOCUMENT_KEYS:
            self.assertIn(key, op)
        self.assertNotIn("/home/", json.dumps(op["input"]))

    def test_persist_reload_and_mode_0600(self):
        op = self._create()
        self.service.mark_running(op["job_id"])
        self.service.update_progress(op["job_id"], current=1, total=3, message="working")
        self.assertTrue(self.service.persist())
        path = operations_path(self.data_path)
        self.assertTrue(path.is_file())
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

        reloaded = OperationService(self.data_path)
        restored = reloaded.get(op["job_id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored["state"], "interrupted")
        self.assertEqual(restored["current"], 1)
        self.assertEqual(restored["total"], 3)

    def test_library_json_unchanged(self):
        before = self.data_path.read_text(encoding="utf-8")
        self._create()
        self.service.persist()
        after = self.data_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_startup_interrupts_active_operations(self):
        op = self._create()
        self.service.mark_running(op["job_id"])
        self.service.persist()
        restarted = OperationService(self.data_path)
        snapshot = restarted.get(op["job_id"])
        self.assertEqual(snapshot["state"], "interrupted")

    def test_cancel_during_promote_refused(self):
        op = self._create(operation_type="setup.commit")
        self.service.mark_running(op["job_id"])
        self.service.set_promote_phase(op["job_id"], active=True)
        with self.assertRaises(JobNotCancellableError):
            self.service.request_cancel(op["job_id"])

    def test_retry_linkage(self):
        original = self._create(operation_type="library.backup")
        self.service.finish(original["job_id"], state="error", error={"code": "X", "message": "fail"})
        retry = self.service.retry(original["job_id"])
        self.assertEqual(retry["retry_of"], original["job_id"])
        self.assertEqual(retry["root_job_id"], original["root_job_id"])
        self.assertNotEqual(retry["job_id"], original["job_id"])

    def test_resume_checkpoint_safe_types(self):
        for operation_type in ("setup.scan", "setup.revalidate", "media.bulk_download"):
            with self.subTest(operation_type=operation_type):
                service = OperationService(self.data_path)
                op = service.create(operation_type=operation_type, title="resume me")
                service.finish(
                    op["job_id"],
                    state="interrupted",
                    checkpoint={"cursor": "abc"},
                )
                snapshot = service.get(op["job_id"])
                self.assertTrue(snapshot["can_resume"])
                resumed = service.resume(op["job_id"])
                self.assertEqual(resumed["resume_of"], op["job_id"])
                self.assertEqual(resumed["checkpoint"], {"cursor": "abc"})

    def test_resume_retry_only_types_blocked(self):
        for operation_type in ("setup.commit", "library.restore", "cloud.sync", "updater.install"):
            with self.subTest(operation_type=operation_type):
                service = OperationService(self.data_path)
                op = service.create(operation_type=operation_type, title="no resume")
                service.finish(op["job_id"], state="interrupted", checkpoint={"step": 1})
                snapshot = service.get(op["job_id"])
                self.assertFalse(snapshot["can_resume"])
                with self.assertRaises(JobNotResumableError):
                    service.resume(op["job_id"])

    def test_retention_prune(self):
        service = OperationService(self.data_path)
        old_finished = []
        for index in range(105):
            op = service.create(operation_type="setup.scan", title=f"job-{index}")
            service.finish(op["job_id"], state="done", result={"completed": 1})
            old_finished.append(op["job_id"])
        service.persist()
        reloaded = OperationService(self.data_path)
        self.assertLessEqual(len(reloaded.list_jobs(limit=200)["jobs"]), 100)

    def test_corrupt_operations_json_discarded_safely(self):
        path = operations_path(self.data_path)
        path.write_text("{not json", encoding="utf-8")
        service = OperationService(self.data_path)
        self.assertEqual(service.list_jobs()["jobs"], [])

    def test_disk_full_does_not_raise(self):
        self._create()
        with mock.patch("pkg.state.operations.atomic_write_text", side_effect=OSError("disk full")):
            self.assertFalse(self.service.persist())

    def test_sse_payload_keys(self):
        op = self._create()
        self.service.mark_running(op["job_id"], phase="scan")
        self.service.update_progress(op["job_id"], current=2, total=5, message="scanning")
        self.service.request_cancel(op["job_id"])
        self.service.finish(op["job_id"], state="cancelled")

        by_kind = {}
        for kind, payload in self.events:
            by_kind.setdefault(kind, []).append(payload)
        self.assertIn(SSE_EVENT_QUEUED, by_kind)
        self.assertIn(SSE_EVENT_PROGRESS, by_kind)
        self.assertIn(SSE_EVENT_CANCELLING, by_kind)
        self.assertIn(SSE_EVENT_FINISHED, by_kind)
        for kind, required in SSE_PAYLOAD_KEYS.items():
            if kind not in by_kind:
                continue
            for payload in by_kind[kind]:
                self.assertEqual(tuple(payload.keys()), required)

    def test_media_bulk_download_retry_failed_only(self):
        op = self._create(operation_type="media.bulk_download", input_data={"game_ids": ["a", "b"]})
        self.service.finish(
            op["job_id"],
            state="partial",
            checkpoint={"failed_game_ids": ["b"], "completed_game_ids": ["a"]},
        )
        retry = self.service.retry(op["job_id"])
        self.assertEqual(retry["input"], {"failed_game_ids": ["b"]})

    def test_list_items_pagination(self):
        op = self._create()
        self.service.add_item_failure(op["job_id"], item_id="1", label="one", state="error", error="boom")
        self.service.add_item_failure(op["job_id"], item_id="2", label="two", state="error")
        page = self.service.list_items(op["job_id"], limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["next_cursor"], "1")
        page2 = self.service.list_items(op["job_id"], cursor=page["next_cursor"], limit=1)
        self.assertEqual(page2["items"][0]["item_id"], "2")

    def test_secrets_not_stored_in_input(self):
        op = self._create(input_data={"api_key": "secret", "preview_id": "p1"})
        self.assertNotIn("api_key", op["input"])
        self.assertEqual(op["input"]["preview_id"], "p1")

    def test_interrupted_sse_event(self):
        op = self._create()
        self.service.finish(op["job_id"], state="interrupted", message="shutdown")
        kinds = [kind for kind, _payload in self.events]
        self.assertIn(SSE_EVENT_INTERRUPTED, kinds)

    def test_list_jobs_cursor_pagination(self):
        ids = []
        for index in range(3):
            op = self._create(title=f"job-{index}")
            ids.append(op["job_id"])
            self.service.finish(op["job_id"], state="done")
        page1 = self.service.list_jobs(limit=2)
        self.assertEqual(len(page1["jobs"]), 2)
        self.assertIsNotNone(page1["next_cursor"])
        page2 = self.service.list_jobs(cursor=page1["next_cursor"], limit=2)
        self.assertEqual(len(page2["jobs"]), 1)

    def test_mark_running_conflict(self):
        op = self._create()
        self.service.finish(op["job_id"], state="done")
        with self.assertRaises(JobStateConflictError):
            self.service.mark_running(op["job_id"])

    def test_update_progress_conflict(self):
        op = self._create()
        self.service.finish(op["job_id"], state="done")
        with self.assertRaises(JobStateConflictError):
            self.service.update_progress(op["job_id"], current=1)

    def test_cancel_not_found_and_inactive(self):
        with self.assertRaises(JobStateConflictError):
            self.service.request_cancel("missing")
        op = self._create()
        self.service.finish(op["job_id"], state="done")
        with self.assertRaises(JobNotCancellableError):
            self.service.request_cancel(op["job_id"])

    def test_finish_invalid_state_and_missing(self):
        op = self._create()
        with self.assertRaises(ValueError):
            self.service.finish(op["job_id"], state="bogus")
        with self.assertRaises(JobStateConflictError):
            self.service.finish("missing", state="done")

    def test_retry_and_resume_conflicts(self):
        op = self._create()
        with self.assertRaises(JobStateConflictError):
            self.service.retry(op["job_id"])
        with self.assertRaises(JobStateConflictError):
            self.service.resume("missing")

    def test_get_operation_service_singleton(self):
        from pkg.state import operations as ops_mod

        ops_mod._SERVICE = None
        first = ops_mod.get_operation_service()
        second = ops_mod.get_operation_service()
        self.assertIs(first, second)

    def test_sse_observer_exception_is_swallowed(self):
        def boom(_kind, _payload):
            raise RuntimeError("observer failed")

        service = OperationService(self.data_path)
        service.set_event_observer(boom)
        service.create(operation_type="setup.scan", title="observer")

    def test_progress_rate_limiting(self):
        op = self._create()
        self.service.mark_running(op["job_id"])
        self.service.update_progress(op["job_id"], current=1, total=10)
        before = operations_path(self.data_path).read_text(encoding="utf-8")
        self.service.update_progress(op["job_id"], current=2, total=10, persist=True)
        after = operations_path(self.data_path).read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_retention_drops_old_completed(self):
        service = OperationService(self.data_path)
        op = service.create(operation_type="setup.scan", title="old")
        record = service.get(op["job_id"])
        record["finished_at"] = "2000-01-01T00:00:00+00:00"
        record["state"] = "done"
        with service._lock:
            service._operations[op["job_id"]] = service._snapshot(record)
        service.persist()
        reloaded = OperationService(self.data_path)
        self.assertIsNone(reloaded.get(op["job_id"]))


if __name__ == "__main__":
    unittest.main()
