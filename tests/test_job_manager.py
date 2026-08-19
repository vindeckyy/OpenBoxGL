#!/usr/bin/env python3
"""Deterministic tests for the JobManager background worker pool."""

import math
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_manager import JobManager, MAX_JOB_NAME_LENGTH

POLL_TIMEOUT = 15.0


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


def _blocked_worker(started, release):
    """Return a worker that signals `started` then waits on `release`."""
    def worker(cancel_event):
        started.set()
        while not release.is_set() and not cancel_event.is_set():
            time.sleep(0.005)
    return worker


class SubmitValidationTests(unittest.TestCase):
    def setUp(self):
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
        old = self.manager.submit("replace", _blocked_worker(started, release))
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
        self.assertEqual(again["state"], "queued")
        self.assertEqual(_wait_for(self.manager, "done-job", {"done"})["state"], "done")


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
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
        self.assertIn("boom", finished["error"])
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
        _wait_for(self.manager, "obs-done", {"done"})
        _wait_for(self.manager, "obs-error", {"error"})
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
        manager = JobManager(max_workers=2, max_jobs=2)
        started = threading.Event()
        release = threading.Event()
        try:
            manager.submit("keep", _blocked_worker(started, release))
            self.assertTrue(started.wait(POLL_TIMEOUT))
            manager.submit("finish", lambda: None)
            self.assertEqual(_wait_for(manager, "finish", {"done"})["state"], "done")
            self.assertEqual(manager.snapshot("keep")["state"], "running")
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


if __name__ == "__main__":
    unittest.main()

