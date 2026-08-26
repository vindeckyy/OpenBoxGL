#!/usr/bin/env python3
"""Deterministic tests for the JobManager background worker pool."""

import math
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_manager import (
    JobManager,
    MAX_JOB_NAME_LENGTH,
    legacy_snapshot_fields,
    operation_title_for_name,
    operation_type_for_name,
)
from pkg.state.operations import OPERATION_DOCUMENT_KEYS, get_operation_service, reset_operation_service_for_tests

POLL_TIMEOUT = 30.0 if os.environ.get("GITHUB_ACTIONS") == "true" else 15.0
_MODULE_TEMPDIR = None
_PREV_DATA_DIR = None


def setUpModule():
    global _MODULE_TEMPDIR, _PREV_DATA_DIR
    _MODULE_TEMPDIR = tempfile.TemporaryDirectory()
    _PREV_DATA_DIR = os.environ.get("OPENBOX_DATA_DIR")
    os.environ["OPENBOX_DATA_DIR"] = _MODULE_TEMPDIR.name
    Path(_MODULE_TEMPDIR.name, "library.json").write_text("{}", encoding="utf-8")
    _reset_operations()


def tearDownModule():
    reset_operation_service_for_tests()
    if _MODULE_TEMPDIR is not None:
        _MODULE_TEMPDIR.cleanup()
    if _PREV_DATA_DIR is None:
        os.environ.pop("OPENBOX_DATA_DIR", None)
    else:
        os.environ["OPENBOX_DATA_DIR"] = _PREV_DATA_DIR


def _reset_operations():
    reset_operation_service_for_tests()
    data_dir = os.environ.get("OPENBOX_DATA_DIR")
    if not data_dir:
        return
    data_path = Path(data_dir) / "library.json"
    data_path.write_text("{}", encoding="utf-8")
    import pkg.state.operations as operations_module

    operations_module._SERVICE = operations_module.OperationService(data_path)


def _wait_for(manager, name, states, timeout=POLL_TIMEOUT):
    """Poll snapshot(name) until its state is in `states` or the deadline passes."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = manager.snapshot(name)
        if last.get("state") in states:
            return last
        time.sleep(0.005)
    return last


def _wait_until(predicate, timeout=POLL_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _blocked_worker(started, release):
    """Return a worker that signals `started` then waits on `release`."""
    def worker(cancel_event):
        started.set()
        while not release.is_set() and not cancel_event.is_set():
            time.sleep(0.005)
    return worker


def _blocked_worker_hold_until_release(started, release):
    """Like _blocked_worker but ignores cancel until `release` is set."""

    def worker(cancel_event):
        started.set()
        while not release.is_set():
            time.sleep(0.005)

    return worker


class SubmitValidationTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=1)

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_name_rejections(self):
        for bad in ("", "   ", "\t", "a\nb", "a\x00b", "x" * (MAX_JOB_NAME_LENGTH + 1)):
            with self.assertRaises(ValueError):
                self.manager.submit(bad, lambda: None)

    def test_max_attempts_is_clamped(self):
        # Keep the job queued behind a blocked worker so the submitted dict
        # still reflects the (possibly clamped) max_attempts value.
        started = threading.Event()
        release = threading.Event()
        self.manager.submit("blocker", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        for raw, expected in ((0, 1), (-5, 1), (3, 3), (7, 5), (99, 5)):
            result = self.manager.submit(f"clamp-{raw}", lambda: None, max_attempts=raw)
            self.assertEqual(result["max_attempts"], expected)
        release.set()

    def test_backoff_rejections(self):
        for index, bad in enumerate(("abc", None, [1.0], math.nan, math.inf, -math.inf, -0.5)):
            with self.assertRaises(ValueError):
                self.manager.submit(f"bad-backoff-{index}", lambda: None, backoff_seconds=bad)

    def test_backoff_is_clamped(self):
        # Clamping is verified via the actual retry delay in BackoffClampTests.
        result = self.manager.submit("huge-backoff", lambda: None, backoff_seconds=9999.0)
        self.assertIsInstance(result, dict)


class SubmitSemanticsTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=1)

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_duplicate_name_returns_existing_job(self):
        started = threading.Event()
        release = threading.Event()
        first = self.manager.submit("same", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        second = self.manager.submit("same", lambda: None)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(second["state"], "running")
        release.set()
        _wait_for(self.manager, "same", {"done", "error", "cancelled"})

    def test_replace_true_with_running_job_sets_old_cancel_event(self):
        started = threading.Event()
        release = threading.Event()
        old = self.manager.submit("replace", _blocked_worker_hold_until_release(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        self.assertEqual(_wait_for(self.manager, "replace", {"running"})["state"], "running")
        replacement = self.manager.submit("replace", lambda: {"replaced": True}, replace=True)
        self.assertNotEqual(replacement["job_id"], old["job_id"])
        # The old worker's cancel event is set synchronously by replace; the
        # old worker exits without overwriting the new job, and the
        # replacement runs to completion.
        self.assertTrue(self.manager._cancel_events[old["job_id"]].is_set())
        release.set()
        self.assertEqual(_wait_for(self.manager, "replace", {"done"})["state"], "done")
        self.assertEqual(self.manager.snapshot("replace")["replaced"], True)
        self.assertEqual(self.manager.snapshot("replace")["job_id"], replacement["job_id"])
        # NOTE: the replaced job's entry in _inflight (and its cancel event)
        # is NOT released here. With max_workers=1 the stale worker is
        # starved and never runs its cleanup, so the old job_id leaks in
        # _inflight (a later submit then raises "queue full" despite no
        # running jobs). This pre-existing behavior is preserved by the
        # Part-2 refactor and reported as an out-of-scope finding.

    def test_queue_full_with_replace(self):
        manager = JobManager(max_workers=1, max_jobs=1)
        started = threading.Event()
        release = threading.Event()
        try:
            manager.submit("only", _blocked_worker(started, release))
            self.assertTrue(started.wait(POLL_TIMEOUT))
            self.assertEqual(_wait_for(manager, "only", {"running"})["state"], "running")
            with self.assertRaises(RuntimeError):
                manager.submit("only", lambda: None, replace=True)
        finally:
            release.set()
            manager.shutdown(wait=True, cancel_futures=True)

    def test_replace_on_finished_job_is_allowed(self):
        self.manager.submit("done-job", lambda: None)
        self.assertEqual(_wait_for(self.manager, "done-job", {"done"})["state"], "done")
        again = self.manager.submit("done-job", lambda: None, replace=True)
        self.assertIn(again["state"], {"queued", "running"})
        self.assertEqual(_wait_for(self.manager, "done-job", {"done"})["state"], "done")


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=1)
        self.captured = []

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_quick_worker_reaches_done(self):
        self.manager.set_observer(self.captured.append)
        job = self.manager.submit("quick", lambda: {"ok": True})
        finished = _wait_for(self.manager, "quick", {"done"})
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["job_id"], job["job_id"])
        self.assertEqual(finished["attempt"], 1)
        self.assertTrue(finished["started_at"])
        self.assertTrue(finished["finished_at"])
        self.assertTrue(finished["finished_at"] >= finished["started_at"])
        self.assertEqual(finished["ok"], True)
        self.assertEqual(finished["error"], "")
        self.assertGreaterEqual(finished["duration_seconds"], 0)
        _wait_until(lambda: bool(self.captured))
        self.assertEqual(self.captured[-1]["name"], "quick")
        self.assertEqual(self.captured[-1]["state"], "done")

    def test_cancel_running_job(self):
        started = threading.Event()
        release = threading.Event()
        job = self.manager.submit("cancel-run", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        self.assertEqual(_wait_for(self.manager, "cancel-run", {"running"})["state"], "running")
        self.assertIn(job["job_id"], self.manager._inflight)
        self.assertTrue(self.manager.cancel("cancel-run"))
        finished = _wait_for(self.manager, "cancel-run", {"cancelled"})
        self.assertEqual(finished["state"], "cancelled")
        self.assertTrue(finished["finished_at"])
        _wait_until(lambda: job["job_id"] not in self.manager._inflight)
        self.assertNotIn(job["job_id"], self.manager._inflight)
        self.assertNotIn(job["job_id"], self.manager._cancel_events)
        release.set()

    def test_cancel_unknown_name_returns_false(self):
        self.assertFalse(self.manager.cancel("missing"))

    def test_worker_signature_dispatch(self):
        received = {}

        def zero_param():
            received["zero"] = "called"
            return {"kind": "zero"}

        def one_param(cancel_event):
            received["one"] = cancel_event
            return {"kind": "one"}

        self.manager.submit("sig-zero", zero_param)
        self.manager.submit("sig-one", one_param)
        self.assertEqual(_wait_for(self.manager, "sig-zero", {"done"})["state"], "done")
        self.assertEqual(_wait_for(self.manager, "sig-one", {"done"})["state"], "done")
        self.assertEqual(received["zero"], "called")
        self.assertIsInstance(received["one"], threading.Event)


class RetryTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=1)
        self.attempts = {"count": 0}

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_failing_worker_exhausts_attempts(self):
        def flaky():
            self.attempts["count"] += 1
            raise RuntimeError("boom")

        self.manager.submit("exhaust", flaky, max_attempts=3, backoff_seconds=0.005)
        finished = _wait_for(self.manager, "exhaust", {"error"})
        self.assertEqual(finished["state"], "error")
        self.assertEqual(finished["attempt"], 3)
        self.assertEqual(finished["max_attempts"], 3)
        self.assertEqual(self.attempts["count"], 3)
        self.assertIn("boom", str(finished["error"]))
        self.assertTrue(finished["finished_at"])

    def test_fail_then_succeed(self):
        def flaky():
            self.attempts["count"] += 1
            if self.attempts["count"] == 1:
                raise RuntimeError("first attempt fails")
            return {"ok": True}

        self.manager.submit("recover", flaky, max_attempts=3, backoff_seconds=0.005)
        finished = _wait_for(self.manager, "recover", {"done"})
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["attempt"], 2)
        self.assertEqual(self.attempts["count"], 2)
        self.assertEqual(finished["ok"], True)


class CancelQueuedTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=1)
        self.started = threading.Event()
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_cancel_queued_job(self):
        self.manager.submit("blocker", _blocked_worker(self.started, self.release))
        self.assertTrue(self.started.wait(POLL_TIMEOUT))
        queued = self.manager.submit("queued", lambda: None)
        self.assertEqual(_wait_for(self.manager, "queued", {"queued"})["state"], "queued")
        self.assertTrue(self.manager.cancel("queued"))
        finished = _wait_for(self.manager, "queued", {"cancelled"})
        self.assertEqual(finished["state"], "cancelled")
        self.assertTrue(finished["finished_at"])
        self.assertNotIn(queued["job_id"], self.manager._inflight)
        self.assertNotIn(queued["job_id"], self.manager._cancel_events)


class ObserverAndHistoryTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=2)
        self.captured = []

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_observer_fires_for_every_terminal_state(self):
        self.manager.set_observer(self.captured.append)

        def explode():
            raise RuntimeError("nope")

        self.manager.submit("obs-done", lambda: {"ok": True})
        self.manager.submit("obs-error", explode, max_attempts=1)
        self.assertEqual(_wait_for(self.manager, "obs-done", {"done"})["state"], "done")
        self.assertEqual(_wait_for(self.manager, "obs-error", {"error"})["state"], "error")
        self.assertTrue(
            _wait_until(lambda: {"obs-done", "obs-error"} <= {entry["name"] for entry in self.captured})
        )
        states = {entry["name"]: entry["state"] for entry in self.captured}
        self.assertEqual(states["obs-done"], "done")
        self.assertEqual(states["obs-error"], "error")

    def test_observer_fires_for_cancelled_job(self):
        started = threading.Event()
        release = threading.Event()
        self.manager.set_observer(self.captured.append)
        self.manager.submit("obs-cancel", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        self.assertTrue(self.manager.cancel("obs-cancel"))
        _wait_for(self.manager, "obs-cancel", {"cancelled"})
        _wait_until(lambda: bool(self.captured))
        self.assertEqual(self.captured[-1]["name"], "obs-cancel")
        self.assertEqual(self.captured[-1]["state"], "cancelled")
        release.set()

    def test_history_newest_first_capped(self):
        manager = JobManager(max_workers=1, history_limit=2)
        try:
            for index in range(3):
                manager.submit(f"h-{index}", lambda: None)
            for index in range(3):
                self.assertEqual(_wait_for(manager, f"h-{index}", {"done"})["state"], "done")
            entries = manager.history()
            self.assertEqual(len(entries), 2)
            self.assertEqual([entry["name"] for entry in entries], ["h-2", "h-1"])
            for entry in entries:
                self.assertEqual(entry["state"], "done")
            self.assertEqual(len(manager._history), 2)
        finally:
            manager.shutdown(wait=True, cancel_futures=True)

    def test_snapshots_and_history_are_copies(self):
        self.manager.submit("copy-a", lambda: None)
        self.manager.submit("copy-b", lambda: None)
        _wait_for(self.manager, "copy-a", {"done"})
        _wait_for(self.manager, "copy-b", {"done"})
        snap = self.manager.snapshots()
        self.assertEqual(set(snap), {"copy-a", "copy-b"})
        snap["copy-a"]["state"] = "mutated"
        self.assertEqual(self.manager.snapshot("copy-a")["state"], "done")


class PruneTests(unittest.TestCase):
    def test_finished_jobs_are_pruned_before_new_submit(self):
        _reset_operations()
        manager = JobManager(max_workers=2, max_jobs=2)
        started = threading.Event()
        release = threading.Event()
        try:
            manager.submit("keep", _blocked_worker(started, release))
            self.assertTrue(started.wait(POLL_TIMEOUT))
            manager.submit("finish", lambda: None)
            self.assertEqual(_wait_for(manager, "finish", {"done"})["state"], "done")
            self.assertEqual(manager.snapshot("keep")["state"], "running")
            self.assertTrue(
                _wait_until(lambda: len(manager._inflight) < manager._max_jobs)
            )
            manager.submit("after", lambda: None)
            self.assertEqual(_wait_for(manager, "after", {"done"})["state"], "done")
            # The finished entry must have been pruned to make room.
            self.assertLessEqual(len(manager._jobs), manager._max_jobs)
            self.assertNotIn("finish", manager._jobs)
            self.assertIn("keep", manager._jobs)
        finally:
            release.set()
            manager.shutdown(wait=True, cancel_futures=True)


class BackoffClampTests(unittest.TestCase):
    def test_retry_delay_is_clamped_to_max_backoff(self):
        _reset_operations()
        # Shrink the clamp so the real inter-attempt wait is measurable in-test.
        with mock.patch("job_manager.MAX_BACKOFF_SECONDS", 0.3):
            manager = JobManager(max_workers=1)
            attempts = {"count": 0, "starts": []}

            def flaky(cancel_event):
                attempts["count"] += 1
                attempts["starts"].append(time.monotonic())
                if attempts["count"] == 1:
                    raise RuntimeError("fail once")
                return {"ok": True}

            try:
                manager.submit("clamped-delay", flaky, max_attempts=2, backoff_seconds=9999.0)
                self.assertEqual(_wait_for(manager, "clamped-delay", {"done"})["state"], "done")
                self.assertEqual(attempts["count"], 2)
                # The backoff between attempts must be clamped to the limit,
                # not the unbounded 9999.0 requested.
                gap = attempts["starts"][1] - attempts["starts"][0]
                self.assertGreaterEqual(gap, 0.29)
                self.assertLessEqual(gap, 0.5)
            finally:
                manager.shutdown(wait=True, cancel_futures=True)


class ProgressStreamingTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=2)
        self.progress_events = []

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_import_job_streaming_events(self):
        self.manager.set_progress_observer(self.progress_events.append)

        def import_worker(context):
            self.assertEqual(context.job_name, "import-streaming")
            self.assertTrue(context.job_id)
            self.assertFalse(context.is_cancelled())
            context.progress(found_count=10, processed_count=0)
            context.progress(found_count=10, processed_count=5)
            context.progress(found_count=10, processed_count=10, status="done")
            return {"imported": 10}

        self.manager.submit("import-streaming", import_worker)
        finished = _wait_for(self.manager, "import-streaming", {"done"})
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["imported"], 10)

        # Verify progress observer captured events
        self.assertGreaterEqual(len(self.progress_events), 3)
        found_counts = [e.get("found_count") for e in self.progress_events]
        self.assertIn(10, found_counts)
        processed = [e.get("processed_count") for e in self.progress_events]
        self.assertEqual(processed, [0, 5, 10])

    def test_job_context_methods_and_edge_cases(self):
        def worker(context):
            # Update progress by job_id
            self.manager.update_progress(context.job_id, custom_metric=42)
            return {"ok": True}

        self.manager.submit("context-test", worker)
        finished = _wait_for(self.manager, "context-test", {"done"})
        self.assertEqual(finished["state"], "done")

        # Update progress on nonexistent or finished job
        self.assertEqual(self.manager.update_progress("nonexistent"), {})
        self.assertEqual(self.manager.update_progress("context-test"), {})

    def test_progress_observer_exception_resilience(self):
        def bad_observer(data):
            raise RuntimeError("observer crash")

        self.manager.set_progress_observer(bad_observer)

        def worker(context):
            context.progress(step=1)
            return {"done": True}

        self.manager.submit("resilient-job", worker)
        finished = _wait_for(self.manager, "resilient-job", {"done"})
        self.assertEqual(finished["state"], "done")


class OperationMappingTests(unittest.TestCase):
    def test_operation_type_and_title_mappings(self):
        self.assertEqual(operation_type_for_name("library-backup"), "library.backup")
        self.assertEqual(operation_type_for_name("emulator-install:pcsx2"), "emulator.install")
        self.assertEqual(operation_type_for_name("update:retroarch"), "emulator.update")
        self.assertEqual(operation_type_for_name("gameyfin:42"), "gameyfin.install")
        self.assertEqual(operation_type_for_name("saves-backup:g1"), "saves.backup")
        self.assertEqual(operation_type_for_name("saves-restore:g1"), "saves.restore")
        self.assertEqual(operation_type_for_name("setup-revalidate:x"), "setup.revalidate")
        self.assertEqual(operation_type_for_name("setup-commit:x"), "setup.commit")
        self.assertEqual(operation_type_for_name("setup-scan:x"), "setup.scan")
        self.assertEqual(operation_type_for_name("unknown-job"), "setup.scan")
        self.assertEqual(operation_title_for_name("emulator-install-all"), "Install all emulators")
        self.assertEqual(operation_title_for_name("emulator-update-all"), "Update all emulators")
        self.assertEqual(operation_title_for_name("emulator-install:pcsx2"), "Install emulator pcsx2")
        self.assertEqual(operation_title_for_name("update:retroarch"), "Update emulator retroarch")
        self.assertEqual(operation_title_for_name("updater-install"), "Install update")
        self.assertEqual(operation_title_for_name("saves-scan"), "Save path scan")
        self.assertEqual(operation_title_for_name("saves-backup"), "Save backup")
        self.assertEqual(operation_title_for_name("saves-restore"), "Save restore")
        self.assertEqual(operation_title_for_name("gameyfin:9"), "Install Gameyfin game 9")
        self.assertEqual(operation_title_for_name("media-bulk"), "Bulk media download")
        self.assertEqual(operation_title_for_name("metadata"), "Metadata database sync")
        self.assertEqual(operation_title_for_name("metadata-match"), "Metadata auto-match")
        self.assertEqual(operation_title_for_name("library-backup"), "Library backup")
        self.assertEqual(operation_title_for_name("cloud-sync"), "Cloud sync")
        self.assertEqual(operation_title_for_name("saves-restore:g1"), "Save restore")
        self.assertEqual(operation_title_for_name("saves-backup:g1"), "Save backup")
        self.assertEqual(operation_title_for_name("custom-job"), "Custom Job")

    def test_legacy_snapshot_fields_additive(self):
        job = {"name": "library-backup", "state": "done", "error": "plain"}
        operation = {
            "job_id": "op-1",
            "type": "library.backup",
            "state": "done",
            "can_cancel": False,
            "can_retry": True,
            "result": {"completed": 1},
            "error": {"code": "X", "message": "fail"},
        }
        snapshot = legacy_snapshot_fields(job, operation)
        self.assertEqual(snapshot["type"], "library.backup")
        self.assertEqual(snapshot["job_id"], "op-1")
        self.assertTrue(snapshot["can_retry"])
        self.assertEqual(snapshot["error"]["code"], "X")

    def test_jobs_handler_helpers(self):
        from api_errors import BadRequest, JobNotCancellable, JobNotResumable, JobStateConflict
        from handlers.jobs import _operation_error, _parse_limit

        self.assertEqual(_parse_limit(None, default=10, maximum=100), 10)
        self.assertEqual(_parse_limit("200", default=10, maximum=100), 100)
        with self.assertRaises(BadRequest):
            _parse_limit("nope", default=10, maximum=100)

        class _Err(Exception):
            def __init__(self, code, message=""):
                super().__init__(message)
                self.code = code

        with self.assertRaises(JobNotCancellable):
            _operation_error(_Err("JOB_NOT_CANCELLABLE", "nope"))
        with self.assertRaises(JobNotResumable):
            _operation_error(_Err("JOB_NOT_RESUMABLE", "nope"))
        with self.assertRaises(JobStateConflict):
            _operation_error(_Err("JOB_STATE_CONFLICT", "conflict"))
        with self.assertRaises(JobStateConflict):
            _operation_error(_Err("OTHER", "fallback"))


class JobManagerCoverageTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=1)

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_bounded_result_encode_failure_returns_empty(self):
        from job_manager import _bounded_result

        with mock.patch("job_manager.json.dumps", side_effect=TypeError("bad")):
            self.assertEqual(_bounded_result({"x": 1}), {})

    def test_update_progress_syncs_operation_fields(self):
        service = get_operation_service()
        self.manager.submit("progress-sync", lambda _ctx: {"ok": True})
        _wait_for(self.manager, "progress-sync", {"done"})
        started = threading.Event()
        release = threading.Event()
        self.manager.submit("progress-live", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        job_id = self.manager.snapshot("progress-live")["job_id"]
        self.manager.update_progress("progress-live", phase="copy", current=2, total=5, message="working")
        op = service.get(job_id)
        self.assertEqual(op["phase"], "copy")
        self.assertEqual(op["current"], 2)
        release.set()

    def test_operation_sync_exception_paths_are_logged(self):
        started = threading.Event()
        release = threading.Event()
        self.manager.submit("sync-ex", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        job_id = self.manager.snapshot("sync-ex")["job_id"]
        with mock.patch("job_manager._get_operation_service") as mock_service:
            mock_service.return_value.update_progress.side_effect = RuntimeError("progress fail")
            self.manager._sync_operation_progress(job_id, phase="x")
            mock_service.return_value.mark_running.side_effect = RuntimeError("run fail")
            self.manager._sync_operation_running(job_id)
            mock_service.return_value.finish.side_effect = RuntimeError("finish fail")
            self.manager._sync_operation_finish(job_id, {"state": "done"})
        release.set()

    def test_terminal_observer_failure_is_resilient(self):
        def bad_observer(_job):
            raise RuntimeError("observer crash")

        self.manager.set_observer(bad_observer)
        with mock.patch.object(self.manager, "_sync_operation_finish", side_effect=RuntimeError("sync fail")):
            self.manager.submit("obs-sync-fail", lambda: None)
            _wait_for(self.manager, "obs-sync-fail", {"done"})

    def test_snapshot_without_operation_id(self):
        with self.manager._lock:
            self.manager._jobs["orphan"] = {"name": "orphan", "state": "done"}
        self.assertEqual(self.manager.snapshot("orphan")["state"], "done")

    def test_history_limit_zero_skips_archive(self):
        manager = JobManager(max_workers=1, history_limit=0)
        try:
            manager.submit("no-history", lambda: None)
            _wait_for(manager, "no-history", {"done"})
            self.assertEqual(manager.history(), [])
        finally:
            manager.shutdown(wait=True, cancel_futures=True)

    def test_submit_queue_full_raises(self):
        manager = JobManager(max_workers=1, max_jobs=1)
        started = threading.Event()
        release = threading.Event()
        try:
            manager.submit("blocker", _blocked_worker(started, release))
            self.assertTrue(started.wait(POLL_TIMEOUT))
            with self.assertRaises(RuntimeError):
                manager.submit("overflow", lambda: None)
        finally:
            release.set()
            manager.shutdown(wait=True, cancel_futures=True)

    def test_executor_submit_failure_cleans_up(self):
        with mock.patch.object(self.manager._executor, "submit", side_effect=RuntimeError("pool dead")):
            with self.assertRaises(RuntimeError):
                self.manager.submit("submit-fail", lambda: None)
        self.assertNotIn("submit-fail", self.manager._jobs)

    def test_worker_signature_inspect_failure_still_runs(self):
        with mock.patch("job_manager.inspect.signature", side_effect=TypeError("bad sig")):
            self.manager.submit("sig-inspect", lambda: {"ok": True})
        self.assertEqual(_wait_for(self.manager, "sig-inspect", {"done"})["ok"], True)

    def test_cancel_running_requests_operation_cancel(self):
        started = threading.Event()
        release = threading.Event()
        self.manager.submit("cancel-run-op", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        with mock.patch("job_manager._get_operation_service") as mock_service:
            mock_service.return_value.request_cancel.side_effect = RuntimeError("cancel fail")
            self.assertTrue(self.manager.cancel("cancel-run-op"))
        release.set()
        _wait_for(self.manager, "cancel-run-op", {"cancelled"})

    def test_cancel_during_retry_backoff_finishes_cancelled(self):
        attempts = {"count": 0}

        def flaky(_cancel_event):
            attempts["count"] += 1
            raise RuntimeError("retry me")

        self.manager.submit("cancel-retry", flaky, max_attempts=3, backoff_seconds=0.2)
        _wait_for(self.manager, "cancel-retry", {"running"})
        self.assertTrue(self.manager.cancel("cancel-retry"))
        finished = _wait_for(self.manager, "cancel-retry", {"cancelled"})
        self.assertEqual(finished["state"], "cancelled")

    def test_shutdown_cancel_futures_cancels_queued_jobs(self):
        manager = JobManager(max_workers=1, max_jobs=2)
        started = threading.Event()
        release = threading.Event()
        try:
            manager.submit("blocker", _blocked_worker(started, release))
            self.assertTrue(started.wait(POLL_TIMEOUT))
            queued = manager.submit("queued-shutdown", lambda: None)
            future = mock.Mock()
            future.cancel.return_value = True
            with manager._lock:
                manager._futures[queued["job_id"]] = future
            manager.shutdown(wait=True, cancel_futures=True)
            self.assertEqual(manager.snapshot("queued-shutdown")["state"], "cancelled")
        finally:
            release.set()


class OperationSyncTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        self.manager = JobManager(max_workers=2)

    def tearDown(self):
        self.manager.shutdown(wait=True, cancel_futures=True)

    def test_submit_links_operation_and_cancel_by_id(self):
        service = get_operation_service()
        self.manager.submit("library-backup", lambda _ctx: {"ok": True})
        finished = _wait_for(self.manager, "library-backup", {"done"})
        op = service.get(finished["job_id"])
        self.assertIsNotNone(op)
        self.assertEqual(op["type"], "library.backup")
        started = threading.Event()
        release = threading.Event()
        self.manager.submit("cloud-sync", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        op_id = self.manager.snapshot("cloud-sync")["job_id"]
        self.assertTrue(self.manager.cancel_by_id(op_id))
        _wait_for(self.manager, "cloud-sync", {"cancelled"})
        release.set()

    def test_finish_syncs_partial_result(self):
        service = get_operation_service()

        def worker(_ctx):
            return {"added": 2, "errors": ["one"]}

        self.manager.submit("metadata-match", worker)
        finished = _wait_for(self.manager, "metadata-match", {"done"})
        op = service.get(finished["job_id"])
        self.assertEqual(op["state"], "partial")
        self.assertEqual(op["result"]["added"], 2)

    def test_operation_sync_error_paths_are_resilient(self):
        with mock.patch("job_manager._get_operation_service", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.manager._operation_for_job("missing"))
        self.manager._sync_operation_progress("", current=1)
        self.manager._sync_operation_running("")
        self.manager._sync_operation_finish("", {})
        with mock.patch("job_manager._get_operation_service") as mock_service:
            mock_service.return_value.update_progress.side_effect = RuntimeError("fail")
            self.manager.submit("saves-scan", lambda _ctx: {"updated": 0})
            _wait_for(self.manager, "saves-scan", {"done"})

    def test_submit_with_explicit_operation_type(self):
        service = get_operation_service()
        self.manager.submit(
            "custom-op",
            lambda _ctx: {"ok": True},
            operation_type="setup.scan",
            input_data={"path": "/games"},
        )
        finished = _wait_for(self.manager, "custom-op", {"done"})
        op = service.get(finished["job_id"])
        self.assertEqual(op["type"], "setup.scan")
        self.assertEqual(op["input"]["path"], "/games")

    def test_job_context_manager_property(self):
        seen = {}

        def worker(ctx):
            seen["manager"] = ctx.manager
            seen["cancelled"] = ctx.is_cancelled()
            return {"ok": True}

        self.manager.submit("ctx-prop", worker)
        _wait_for(self.manager, "ctx-prop", {"done"})
        self.assertIs(seen["manager"], self.manager)
        self.assertFalse(seen["cancelled"])

    def test_finish_maps_error_state_and_bounded_result(self):
        from job_manager import _bounded_result

        service = get_operation_service()

        def partial_counts(_ctx):
            return {"added": "not-int", "errors": ["x"]}

        def explode(_ctx):
            raise RuntimeError("boom")

        self.manager.submit("library-restore", partial_counts)
        finished = _wait_for(self.manager, "library-restore", {"done"})
        op = service.get(finished["job_id"])
        self.assertEqual(op["state"], "partial")
        self.manager.submit("cloud-sync", explode, max_attempts=1)
        _wait_for(self.manager, "cloud-sync", {"error"})
        huge = {"blob": "x" * (70 * 1024)}
        bounded = _bounded_result(huge)
        self.assertTrue(bounded.get("result_truncated"))


class FacadeSyncTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        import webapp_state

        self.webapp_state = webapp_state
        webapp_state.INSTALLS.clear()
        webapp_state.METADATA_JOB.clear()
        webapp_state.MEDIA_JOB.clear()
        self.manager = webapp_state.JOB_MANAGER

    def tearDown(self):
        self.webapp_state.INSTALLS.clear()
        self.webapp_state.METADATA_JOB.clear()
        self.webapp_state.MEDIA_JOB.clear()

    def test_installs_facade_syncs_operation_progress(self):
        from webapp_state import INSTALLS

        started = threading.Event()
        release = threading.Event()
        self.manager.submit("gameyfin:7", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        INSTALLS["gameyfin:7"] = {
            "state": "installing",
            "current": 1,
            "total": 3,
            "message": "working",
        }
        job_id = self.manager.get_operation_id("gameyfin:7")
        op = get_operation_service().get(job_id)
        self.assertEqual(op["state"], "running")
        self.assertEqual(op["current"], 1)
        release.set()
        _wait_for(self.manager, "gameyfin:7", {"done", "error", "cancelled"})

    def test_media_job_facade_allows_checkpoint_keys(self):
        from webapp_state import MEDIA_JOB

        started = threading.Event()
        release = threading.Event()
        self.manager.submit("media-bulk", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        MEDIA_JOB.update({
            "state": "downloading",
            "completed_game_ids": ["g1"],
            "failed_game_ids": ["g2"],
        })
        job_id = self.manager.get_operation_id("media-bulk")
        op = get_operation_service().get(job_id)
        checkpoint = (op or {}).get("checkpoint") or {}
        self.assertEqual(checkpoint.get("completed_game_ids"), ["g1"])
        self.assertEqual(checkpoint.get("failed_game_ids"), ["g2"])
        release.set()
        _wait_for(self.manager, "media-bulk", {"done", "error", "cancelled"})

    def test_installs_job_name_branches(self):
        from webapp_state import INSTALLS, _installs_job_name

        self.assertEqual(_installs_job_name("update:ra"), "update:ra")
        self.assertEqual(_installs_job_name("__all__"), "emulator-install-all")
        self.assertEqual(_installs_job_name("__update_all__"), "emulator-update-all")
        self.assertEqual(_installs_job_name("pcsx2"), "emulator-install:pcsx2")
        started = threading.Event()
        release = threading.Event()
        self.manager.submit(_installs_job_name("update:ra"), _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        INSTALLS["update:ra"] = {"state": "updating", "message": "patching"}
        INSTALLS.update({"pcsx2": {"state": "installing"}})
        self.assertEqual(INSTALLS["pcsx2"]["state"], "installing")
        release.set()

    def test_metadata_facade_uses_alternate_job_name(self):
        from webapp_state import METADATA_JOB

        started = threading.Event()
        release = threading.Event()
        self.manager.submit("metadata-match", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        METADATA_JOB.update({"state": "running", "current": 2, "total": 5})
        job_id = self.manager.get_operation_id("metadata-match")
        op = get_operation_service().get(job_id)
        self.assertEqual(op["current"], 2)
        release.set()


class JobsAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        Path(cls.tempdir.name, "library.json").write_text("{}", encoding="utf-8")
        reset_operation_service_for_tests()
        import pkg.state.operations as operations_module
        operations_module._SERVICE = operations_module.OperationService(Path(cls.tempdir.name) / "library.json")
        import web_app
        from http.server import ThreadingHTTPServer

        cls.web_app = web_app
        web_app.TOKEN = "jobs-adapter-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        reset_operation_service_for_tests()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def setUp(self):
        reset_operation_services()
        import webapp_state
        webapp_state.INSTALLS.clear()
        webapp_state.METADATA_JOB.clear()
        webapp_state.MEDIA_JOB.clear()

    def request(self, method, path, payload=None):
        import json
        import urllib.request

        data = None
        headers = {"X-OpenBox-Token": "jobs-adapter-token"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"error": body}
            return error.code, parsed

    def test_legacy_jobs_shape(self):
        status, payload = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertIn("jobs", payload)
        self.assertIn("history", payload)

    def test_v2_jobs_list_keys(self):
        service = get_operation_service()
        created = service.create(operation_type="setup.commit", title="Commit")
        service.finish(created["job_id"], state="error", error={"code": "X", "message": "fail"})
        status, payload = self.request("GET", "/api/v2/jobs")
        self.assertEqual(status, 200)
        self.assertIn("cursor", payload)
        self.assertIn("next_cursor", payload)
        self.assertTrue(payload["jobs"])
        for key in OPERATION_DOCUMENT_KEYS:
            self.assertIn(key, payload["jobs"][0])

    def test_v2_cancel_not_cancellable(self):
        service = get_operation_service()
        created = service.create(operation_type="setup.commit", title="Commit")
        service.mark_running(created["job_id"])
        service.set_promote_phase(created["job_id"], active=True)
        status, payload = self.request("POST", "/api/v2/jobs/cancel", {"job_id": created["job_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "JOB_NOT_CANCELLABLE")

    def test_v2_resume_not_resumable(self):
        service = get_operation_service()
        created = service.create(operation_type="setup.commit", title="Commit")
        service.finish(created["job_id"], state="interrupted")
        status, payload = self.request("POST", "/api/v2/jobs/resume", {"job_id": created["job_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "JOB_NOT_RESUMABLE")

    def test_v2_resume_checkpoint_media_bulk(self):
        service = get_operation_service()
        created = service.create(
            operation_type="media.bulk_download",
            title="Bulk",
            checkpoint={"completed_game_ids": ["g1"], "failed_game_ids": ["g2"]},
        )
        service.finish(created["job_id"], state="interrupted")
        status, payload = self.request("POST", "/api/v2/jobs/resume", {"job_id": created["job_id"]})
        self.assertEqual(status, 202)
        self.assertEqual(payload["state"], "queued")
        self.assertEqual(payload["resume_of"], created["job_id"])

    def test_v1_jobs_route_matches_legacy_shape(self):
        status, payload = self.request("GET", "/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertIn("jobs", payload)
        self.assertIn("history", payload)

    def test_v2_jobs_items_wire_contract(self):
        service = get_operation_service()
        created = service.create(operation_type="media.bulk_download", title="Bulk")
        service.add_item_failure(
            created["job_id"],
            item_id="item-1",
            label="Game 1",
            state="failed",
            error={"code": "X", "message": "fail"},
        )
        status, payload = self.request("GET", f"/api/v2/jobs/items?job_id={created['job_id']}")
        self.assertEqual(status, 200)
        for key in ("job_id", "cursor", "next_cursor", "items"):
            self.assertIn(key, payload)
        self.assertEqual(payload["job_id"], created["job_id"])
        self.assertTrue(payload["items"])
        item = payload["items"][0]
        for key in ("item_id", "label", "state", "error"):
            self.assertIn(key, item)

    def test_v2_jobs_items_validation(self):
        status, payload = self.request("GET", "/api/v2/jobs/items")
        self.assertEqual(status, 400)
        status, payload = self.request("GET", "/api/v2/jobs/items?job_id=missing-job")
        self.assertEqual(status, 404)
        status, payload = self.request("GET", "/api/v2/jobs?limit=not-a-number")
        self.assertEqual(status, 400)

    def test_v2_cancel_success(self):
        service = get_operation_service()
        created = service.create(operation_type="setup.scan", title="Scan")
        service.mark_running(created["job_id"])
        status, payload = self.request("POST", "/api/v2/jobs/cancel", {"job_id": created["job_id"]})
        self.assertEqual(status, 202)
        self.assertEqual(payload["job_id"], created["job_id"])
        self.assertEqual(payload["state"], "cancelling")

    def test_v2_retry_wire_contract(self):
        service = get_operation_service()
        created = service.create(operation_type="library.backup", title="Backup")
        service.finish(created["job_id"], state="error", error={"code": "X", "message": "fail"})
        status, payload = self.request("POST", "/api/v2/jobs/retry", {"job_id": created["job_id"]})
        self.assertEqual(status, 202)
        for key in ("job_id", "root_job_id", "retry_of", "state"):
            self.assertIn(key, payload)
        self.assertEqual(payload["retry_of"], created["job_id"])
        self.assertEqual(payload["state"], "queued")

    def test_v2_retry_with_custom_input(self):
        service = get_operation_service()
        created = service.create(
            operation_type="media.bulk_download",
            title="Bulk",
            input_data={"game_ids": ["a"]},
        )
        service.finish(created["job_id"], state="error", error={"code": "X", "message": "fail"})
        status, payload = self.request(
            "POST",
            "/api/v2/jobs/retry",
            {"job_id": created["job_id"], "input": {"failed_game_ids": ["a"]}},
        )
        self.assertEqual(status, 202)
        self.assertNotEqual(payload["job_id"], created["job_id"])

    def test_v2_retry_conflict_maps_to_job_state_conflict(self):
        service = get_operation_service()
        created = service.create(operation_type="setup.scan", title="Scan")
        service.mark_running(created["job_id"])
        status, payload = self.request("POST", "/api/v2/jobs/retry", {"job_id": created["job_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "JOB_STATE_CONFLICT")

    def test_v2_resume_setup_commit_not_resumable(self):
        service = get_operation_service()
        created = service.create(operation_type="setup.commit", title="Commit")
        service.finish(created["job_id"], state="interrupted")
        status, payload = self.request("POST", "/api/v2/jobs/resume", {"job_id": created["job_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "JOB_NOT_RESUMABLE")

    def test_legacy_jobs_include_operation_fields(self):
        import webapp_state

        webapp_state.JOB_MANAGER.submit("library-backup", lambda _ctx: {"archive": "x.zip"})
        _wait_for(webapp_state.JOB_MANAGER, "library-backup", {"done"})
        status, payload = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        snapshot = payload["jobs"]["library-backup"]
        self.assertIn("job_id", snapshot)
        self.assertIn("type", snapshot)
        self.assertIn("can_cancel", snapshot)

    def test_backup_create_wraps_operation(self):
        from openbox import save_state

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        status, payload = self.request("POST", "/api/backup/create", {"items": ["library"], "keep": 1})
        self.assertEqual(status, 202)
        self.assertIn("job_id", payload)
        op = get_operation_service().get(payload["job_id"])
        self.assertIsNotNone(op)
        self.assertEqual(op["type"], "library.backup")

    def test_cloud_sync_wraps_operation(self):
        with mock.patch("handlers.health.sync_cloud", return_value={"synced": True}):
            status, payload = self.request("POST", "/api/cloud/sync", {})
        self.assertEqual(status, 202)
        self.assertIn("job_id", payload)
        op = get_operation_service().get(payload["job_id"])
        self.assertEqual(op["type"], "cloud.sync")

    def test_saves_backup_and_scan_wrap_operations(self):
        from openbox import DATA, save_state

        save_root = DATA.parent / "save-fixture"
        save_root.mkdir(parents=True, exist_ok=True)
        (save_root / "slot.sav").write_text("data")
        save_state({
            "games": [{
                "name": "G",
                "path": "/bin/true",
                "game_id": "g1",
                "save_paths": [str(save_root)],
            }],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        backup_status, backup_payload = self.request("POST", "/api/saves/backup", {"id": 0})
        self.assertEqual(backup_status, 202)
        self.assertIn("job_id", backup_payload)
        scan_status, scan_payload = self.request("POST", "/api/saves/scan/apply", {})
        self.assertEqual(scan_status, 202)
        self.assertIn("job_id", scan_payload)
        import webapp_state
        with mock.patch("handlers.data.scan_all_saves", return_value={0: ["/tmp/scan-save"]}):
            _wait_for(webapp_state.JOB_MANAGER, "saves-scan", {"done"})

    def test_gameyfin_install_creates_operation(self):
        from openbox import save_state

        save_state({
            "games": [{"name": "G", "gameyfin_id": "11"}],
            "profiles": {},
            "history": [],
            "settings": {"gameyfin_url": "http://gameyfin.local"},
            "playlists": [],
        })
        with mock.patch(
            "handlers.data.install_gameyfin_game",
            return_value={"gameyfin_id": "11", "store_installed": True, "path": "/tmp/x", "launch": "/tmp/x"},
        ):
            status, payload = self.request("POST", "/api/gameyfin/install", {"gameyfin_id": "11"})
        self.assertEqual(status, 202)
        job_id = payload.get("job_id")
        self.assertTrue(job_id)
        op = get_operation_service().get(job_id)
        self.assertEqual(op["type"], "gameyfin.install")

    def test_v2_jobs_cancel_retry_resume_require_job_id(self):
        for path in ("/api/v2/jobs/cancel", "/api/v2/jobs/retry", "/api/v2/jobs/resume"):
            status, _payload = self.request("POST", path, {})
            self.assertEqual(status, 400, path)

    def test_v2_retry_unknown_job_with_input(self):
        status, _payload = self.request("POST", "/api/v2/jobs/retry", {"job_id": "missing", "input": {}})
        self.assertEqual(status, 404)

    def test_v2_resume_unknown_job(self):
        status, _payload = self.request("POST", "/api/v2/jobs/resume", {"job_id": "missing"})
        self.assertEqual(status, 404)

    def test_v2_cancel_unknown_job(self):
        status, _payload = self.request("POST", "/api/v2/jobs/cancel", {"job_id": "missing"})
        self.assertEqual(status, 404)

    def test_restore_library_backup_wraps_operation(self):
        from openbox import DATA

        archive = DATA.parent / "backups" / "OpenBoxBackup-test.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"PK")
        with mock.patch("handlers.health.restore_backup", return_value={"library": True}):
            status, payload = self.request("POST", "/api/backup/restore", {"path": str(archive)})
        self.assertEqual(status, 202)
        self.assertIn("job_id", payload)

    def test_restore_library_backup_bumps_media_epoch(self):
        import webapp_state
        from openbox import DATA

        archive = DATA.parent / "backups" / "OpenBoxBackup-media.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"PK")
        with mock.patch("handlers.health.restore_backup", return_value={"library": True, "media": True}), \
             mock.patch("handlers.health.bump_media_epoch") as bump:
            status, payload = self.request("POST", "/api/backup/restore", {"path": str(archive)})
            self.assertEqual(status, 202)
            _wait_for(webapp_state.JOB_MANAGER, "library-restore", {"done"})
            bump.assert_called_once()

    def test_update_install_wraps_operation(self):
        import webapp_state

        with mock.patch("handlers.health.check_update", return_value={"version": "9.9.9"}), \
             mock.patch("handlers.health.install_update", return_value={"installed": True}):
            status, payload = self.request("POST", "/api/update/install", {})
        self.assertEqual(status, 202)
        self.assertIn("job_id", payload)
        _wait_for(webapp_state.JOB_MANAGER, "updater-install", {"done"})
        op = get_operation_service().get(payload["job_id"])
        self.assertEqual(op["type"], "updater.install")

    def test_data_handler_routes(self):
        from pathlib import Path

        from openbox import DATA, save_state

        doc = DATA.parent / "docs" / "manual.pdf"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"%PDF-test")
        save_state({
            "games": [{
                "name": "G",
                "path": "/bin/true",
                "game_id": "g1",
                "save_paths": [str(DATA.parent / "saves")],
                "documents": [{"name": "manual", "path": str(doc)}],
                "gameyfin_id": "5",
            }],
            "profiles": {},
            "history": [],
            "settings": {
                "platform_documents": {"NES": [{"name": "doc", "path": str(doc)}]},
                "gameyfin_url": "http://gameyfin.local",
            },
            "playlists": [],
        })
        (DATA.parent / "saves").mkdir(parents=True, exist_ok=True)
        routes = [
            ("GET", "/api/saves?id=0", None),
            ("GET", "/api/saves/discover?id=0", None),
            ("GET", "/api/saves/scan", None),
            ("GET", "/api/highscores?id=0", None),
            ("GET", "/api/platform/documents?platform=NES", None),
            ("GET", "/api/platform/documents", None),
            ("GET", "/api/gameyfin/install/status?gameyfin_id=5", None),
            ("GET", "/api/save-tools/status", None),
        ]
        for method, path, body in routes:
            status, payload = self.request(method, path, body)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(payload, dict, path)
        with mock.patch("handlers.data.catalog_gameyfin", return_value=([], [])):
            status, _payload = self.request("GET", "/api/gameyfin/providers", None)
            self.assertEqual(status, 200)
        with mock.patch("handlers.data.test_gameyfin_connection", return_value={"ok": True}):
            status, _payload = self.request("POST", "/api/gameyfin/test", {"gameyfin_url": "http://x"})
            self.assertEqual(status, 200)
        with mock.patch("handlers.data.run_ludusavi", return_value={"ok": True}):
            status, _payload = self.request("POST", "/api/save-tools/ludusavi", {"action": "backup", "id": 0})
            self.assertEqual(status, 200)
        with mock.patch("handlers.data.run_hoard", return_value={"ok": True}):
            status, _payload = self.request("POST", "/api/save-tools/hoard", {"action": "backup", "id": 0})
            self.assertEqual(status, 200)
        with mock.patch("handlers.data.export_highscores", return_value={"exported": 1}):
            status, _payload = self.request("POST", "/api/highscores/export", {"id": 0})
            self.assertEqual(status, 200)
        with mock.patch("handlers.data.import_highscores", return_value=1):
            status, _payload = self.request("POST", "/api/highscores/import", {"id": 0, "path": str(DATA.parent)})
            self.assertEqual(status, 200)
        status, _payload = self.request("POST", "/api/platform/documents", {"platform": "NES", "documents": []})
        self.assertEqual(status, 200)
        status, _payload = self.request("POST", "/api/saves/add", {"id": 0, "path": str(DATA.parent / "saves")})
        self.assertEqual(status, 200)
        with mock.patch("handlers.data.restore_saves", return_value=Path("backup.zip")):
            status, payload = self.request("POST", "/api/saves/restore", {"id": 0, "backup": "backup.zip"})
            self.assertEqual(status, 202)
            self.assertIn("job_id", payload)
        with mock.patch("handlers.data.uninstall_gameyfin_game", return_value={"removed": True}):
            status, _payload = self.request("POST", "/api/gameyfin/uninstall", {"id": 0})
            self.assertEqual(status, 200)

    def test_data_handler_error_paths(self):
        status, _payload = self.request("GET", "/api/saves?id=99", None)
        self.assertEqual(status, 404)
        status, _payload = self.request("GET", "/api/saves/discover?id=99", None)
        self.assertEqual(status, 404)
        status, _payload = self.request("GET", "/api/highscores?id=99", None)
        self.assertEqual(status, 404)
        status, _payload = self.request("GET", "/api/gameyfin/install/status", None)
        self.assertEqual(status, 400)

    def test_gameyfin_install_branches(self):
        from openbox import save_state
        from web_app import Handler

        save_state({
            "games": [{"name": "G", "gameyfin_id": "12"}],
            "profiles": {},
            "history": [],
            "settings": {"gameyfin_url": "http://gameyfin.local"},
            "playlists": [],
        })
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        with self.assertRaises(ValueError):
            Handler.install_gameyfin(handler, {})
        with mock.patch("handlers.data.install_gameyfin_game", return_value={"gameyfin_id": "12"}):
            Handler.install_gameyfin(handler, {"gameyfin_id": "12"})
            Handler.install_gameyfin(handler, {"gameyfin_id": "12"})
        self.assertEqual(handler.send_json.call_args_list[-1][0][0], 200)

    def test_facade_sync_handles_operation_errors(self):
        import webapp_state
        from webapp_state import INSTALLS

        started = threading.Event()
        release = threading.Event()
        webapp_state.JOB_MANAGER.submit("gameyfin:99", _blocked_worker(started, release))
        self.assertTrue(started.wait(POLL_TIMEOUT))
        with mock.patch("pkg.state.operations.get_operation_service") as mock_service:
            mock_service.return_value.mark_running.side_effect = RuntimeError("sync fail")
            mock_service.return_value.update_progress.side_effect = RuntimeError("sync fail")
            INSTALLS["gameyfin:99"] = {"state": "installing", "current": 1, "total": 2}
        release.set()


class DataHandlersStreamTests(unittest.TestCase):
    def setUp(self):
        _reset_operations()
        from openbox import DATA, save_state

        self.doc = DATA.parent / "docs" / "manual.pdf"
        self.doc.parent.mkdir(parents=True, exist_ok=True)
        self.doc.write_bytes(b"%PDF-test")
        save_state({
            "games": [{
                "name": "G",
                "path": "/bin/true",
                "game_id": "g1",
                "documents": [{"name": "manual", "path": str(self.doc)}],
            }],
            "profiles": {},
            "history": [],
            "settings": {"platform_documents": {"NES": [{"name": "doc", "path": str(self.doc)}]}},
            "playlists": [],
        })
        from web_app import Handler

        self.handler = object.__new__(Handler)

    def test_document_routes_stream_bytes(self):
        import io
        from urllib.parse import urlparse

        self.handler.wfile = io.BytesIO()
        self.handler.send_response = mock.Mock()
        self.handler.headers_common = mock.Mock()
        self.handler.send_header = mock.Mock()
        self.handler.end_headers = mock.Mock()
        with mock.patch("handlers.data.safe_document_file", return_value=self.doc):
            self.handler._api_get_api_document(urlparse("/api/document?id=0&index=0"))
            self.assertTrue(self.handler.wfile.getvalue())
            self.handler.wfile = io.BytesIO()
            self.handler._api_get_api_platform_document(urlparse("/api/platform/document?platform=NES&index=0"))
            self.assertTrue(self.handler.wfile.getvalue())

    def test_gameyfin_password_and_install_error_paths(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        with mock.patch("handlers.data.test_gameyfin_connection", return_value={"ok": True}) as conn:
            Handler.test_gameyfin(handler, {"gameyfin_password": ""})
            conn.assert_called_once()
        with mock.patch("handlers.data.install_gameyfin_game", side_effect=OSError("download failed")):
            Handler.install_gameyfin(handler, {"gameyfin_id": "44", "library_id": 0})
        self.assertEqual(handler.send_json.call_args[0][0], 202)
        with self.assertRaises(ValueError):
            Handler.uninstall_gameyfin(handler, {"id": 0})


def reset_operation_services():
    reset_operation_service_for_tests()
    data_dir = os.environ.get("OPENBOX_DATA_DIR")
    if not data_dir:
        return
    import pkg.state.operations as operations_module
    operations_module._SERVICE = operations_module.OperationService(Path(data_dir) / "library.json")


if __name__ == "__main__":
    unittest.main()

