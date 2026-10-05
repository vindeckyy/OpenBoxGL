#!/usr/bin/env python3
"""S11 -- superseding a job stranded its operation, and cancelling by id hit
the wrong job.

Two defects in `JobManager`, both about state that outlives the job it belongs
to.

**1. `replace=True` discarded a queued job silently.** `submit` set the old
cancel event and popped `self._jobs[name]`. The worker for the old job then hit
``_run_job``'s identity check -- ``current.get("job_id") != job_id`` -- and
returned at once, before the notification that marks an operation terminal. No
path ever closed that operation, so its row stayed ``queued``/``running``
permanently, spinner included. `web_app._health_rescan_tick` and the
user-initiated library scan both submit the health scan with ``replace=True``,
so a user pressing "scan" could strand the background rescan's operation
forever.

**2. `cancel_by_id` cancelled whatever job now held that name.** The
``_names_by_id`` map was written on submit and popped only when the executor
submit itself failed -- never on completion. So a finished operation's id still
resolved to its name, and ``cancel_by_id`` killed a *different, currently
running* job that happened to have taken the name. It was also an unbounded
per-job leak for the life of the process.

The invariant: **an operation reaches a terminal state exactly once, and a
finished job's id stops resolving to a name.**
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_manager import JobManager
from pkg.state.operations import get_operation_service, reset_operation_service_for_tests

POLL_TIMEOUT = 15.0
_MODULE_TEMPDIR = None
_PREV_DATA_DIR = None


def setUpModule():
    global _MODULE_TEMPDIR, _PREV_DATA_DIR
    _MODULE_TEMPDIR = tempfile.TemporaryDirectory()
    _PREV_DATA_DIR = os.environ.get("OPENBOX_DATA_DIR")
    os.environ["OPENBOX_DATA_DIR"] = _MODULE_TEMPDIR.name
    Path(_MODULE_TEMPDIR.name, "library.json").write_text("{}", encoding="utf-8")
    reset_operation_service_for_tests()


def tearDownModule():
    reset_operation_service_for_tests()
    if _MODULE_TEMPDIR is not None:
        _MODULE_TEMPDIR.cleanup()
    if _PREV_DATA_DIR is None:
        os.environ.pop("OPENBOX_DATA_DIR", None)
    else:
        os.environ["OPENBOX_DATA_DIR"] = _PREV_DATA_DIR


def _wait_for(predicate, timeout=POLL_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Gate:
    """A worker that blocks until released, so a job can be observed mid-flight."""

    def __init__(self, test):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        # Never leave a test waiting out the full poll timeout on cleanup.
        test.addCleanup(self.release.set)

    def __call__(self):
        self.calls += 1
        self.started.set()
        self.release.wait(POLL_TIMEOUT)
        return {"completed": 1}


class _Instant:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return {"completed": 1}


class JobReplaceTests(unittest.TestCase):
    def setUp(self):
        reset_operation_service_for_tests()
        self.manager = JobManager(max_workers=4, max_jobs=16)
        self.addCleanup(self.manager.shutdown)
        self.service = get_operation_service()

    def _operation(self, job_id):
        return self.service.get(job_id)

    def test_a_superseded_job_reaches_a_terminal_operation_state(self):
        """The reported bug: the operation row stays queued/running forever."""
        first = _Gate(self)
        self.manager.submit("health_scan", first, operation_type="health_scan")
        self.assertTrue(first.started.wait(POLL_TIMEOUT), "the first job never started")
        first_id = self.manager.get_operation_id("health_scan")

        self.manager.submit("health_scan", _Instant(), replace=True, operation_type="health_scan")

        finished = _wait_for(
            lambda: (self._operation(first_id) or {}).get("state") in
            {"done", "error", "cancelled", "partial"}
        )
        self.assertTrue(
            finished,
            f"the superseded operation is still {self._operation(first_id).get('state')!r}; "
            "it would show a spinner forever",
        )
        first.release.set()

    def test_the_superseded_worker_never_runs_to_completion(self):
        first = _Gate(self)
        self.manager.submit("health_scan", first, operation_type="health_scan")
        self.assertTrue(first.started.wait(POLL_TIMEOUT))
        self.manager.submit("health_scan", _Instant(), replace=True, operation_type="health_scan")
        time.sleep(0.2)
        self.assertEqual(first.calls, 1, "the superseded worker was invoked more than once")
        first.release.set()

    def test_the_replacement_job_still_completes_normally(self):
        self.manager.submit("health_scan", _Gate(self), operation_type="health_scan")
        self.manager.submit("health_scan", _Instant(), replace=True, operation_type="health_scan")
        new_id = self.manager.get_operation_id("health_scan")
        self.assertTrue(
            _wait_for(lambda: (self._operation(new_id) or {}).get("state") in {"done", "partial"}),
            f"the replacement operation ended as {self._operation(new_id).get('state')!r}",
        )

    def test_replacing_an_already_finished_job_does_not_rename_a_stale_operation(self):
        """A finished job has no operation left to cancel."""
        self.manager.submit("health_scan", _Instant(), operation_type="health_scan")
        first_id = self.manager.get_operation_id("health_scan")
        self.assertTrue(
            _wait_for(lambda: (self._operation(first_id) or {}).get("state") in {"done", "partial"})
        )
        # This must not touch the already-terminal operation.
        self.manager.submit("health_scan", _Instant(), replace=True, operation_type="health_scan")
        self.assertEqual(
            (self._operation(first_id) or {}).get("state") in {"done", "partial"}, True
        )

    def test_submitting_without_replace_leaves_the_running_job_alone(self):
        gate = _Gate(self)
        self.manager.submit("health_scan", gate, operation_type="health_scan")
        self.assertTrue(gate.started.wait(POLL_TIMEOUT))
        job_id = self.manager.get_operation_id("health_scan")
        self.manager.submit("health_scan", _Instant(), operation_type="health_scan")
        self.assertEqual(
            self.manager.get_operation_id("health_scan"), job_id,
            "a second submit without replace must not create a new operation",
        )
        gate.release.set()


class JobCancelByIdTests(unittest.TestCase):
    def setUp(self):
        reset_operation_service_for_tests()
        self.manager = JobManager(max_workers=4, max_jobs=16)
        self.addCleanup(self.manager.shutdown)
        self.service = get_operation_service()

    def test_a_finished_jobs_id_no_longer_resolves_to_a_name(self):
        self.manager.submit("health_scan", _Instant(), operation_type="health_scan")
        job_id = self.manager.get_operation_id("health_scan")
        self.assertTrue(
            _wait_for(lambda: (self.service.get(job_id) or {}).get("state") in {"done", "partial"}),
            "the job never finished",
        )
        # The mapping is dropped when the job turns terminal, before observers
        # see it, so it is already gone the moment the operation reads done.
        self.assertIsNone(
            self.manager.name_for_operation_id(job_id),
            "a finished job's id still resolves to a name, so cancel_by_id can hit a new job",
        )

    def test_cancelling_a_finished_operation_does_not_cancel_a_new_job(self):
        """The reported bug: cancelling operation A killed operation B."""
        _Gate(self)
        self.manager.submit("health_scan", _Instant(), operation_type="health_scan")
        first_id = None
        # Run a quick job, let it finish, then reuse the same name.
        _wait_for(lambda: True)
        first_id = self.manager.get_operation_id("health_scan")
        _wait_for(lambda: (self.service.get(first_id) or {}).get("state") in {"done", "partial"})

        # A different job now holds the name.
        second = _Gate(self)
        self.manager.submit("health_scan", second, replace=True, operation_type="health_scan")
        self.assertTrue(second.started.wait(POLL_TIMEOUT), "the second job never started")
        second_id = self.manager.get_operation_id("health_scan")
        self.assertNotEqual(second_id, first_id)

        cancelled = self.manager.cancel_by_id(first_id)
        self.assertFalse(cancelled, "cancelling a finished operation reported success")
        time.sleep(0.2)
        self.assertEqual(
            self.manager.snapshot("health_scan").get("state"), "running",
            "cancelling a finished operation killed the unrelated running job",
        )
        second.release.set()

    def test_cancelling_a_running_operation_by_id_still_works(self):
        gate = _Gate(self)
        self.manager.submit("health_scan", gate, operation_type="health_scan")
        self.assertTrue(gate.started.wait(POLL_TIMEOUT))
        job_id = self.manager.get_operation_id("health_scan")
        self.assertTrue(self.manager.cancel_by_id(job_id), "cancelling a live job failed")

        # Cancellation is cooperative: the operation reads "cancelling" until
        # the worker returns, so it can only reach "cancelled" once the gate
        # opens. Asserting a terminal state before then would be asserting that
        # the manager kills threads, which it does not and should not.
        self.assertIn(
            (self.service.get(job_id) or {}).get("state"), {"cancelling", "cancelled"},
        )
        gate.release.set()
        self.assertTrue(
            _wait_for(lambda: (self.service.get(job_id) or {}).get("state") == "cancelled"),
            f"a cancelled operation ended as {(self.service.get(job_id) or {}).get('state')!r}",
        )

    def test_an_unknown_id_is_rejected(self):
        self.assertFalse(self.manager.cancel_by_id("no-such-operation"))
        self.assertIsNone(self.manager.name_for_operation_id("no-such-operation"))


class NameMapDoesNotLeakTests(unittest.TestCase):
    def setUp(self):
        reset_operation_service_for_tests()
        self.manager = JobManager(max_workers=2, max_jobs=64)
        self.addCleanup(self.manager.shutdown)

    def test_the_id_to_name_map_does_not_grow_with_completed_jobs(self):
        """It was popped only on submit failure, so it grew by one per job."""
        for _ in range(12):
            self.manager.submit("health_scan", _Instant(), operation_type="health_scan")
            _wait_for(
                lambda: self.manager.snapshot("health_scan").get("state") in
                {"done", "error", "cancelled", "partial"},
                timeout=5.0,
            )
        _wait_for(lambda: not self.manager._names_by_id, timeout=5.0)
        self.assertEqual(
            self.manager._names_by_id, {},
            f"the id->name map retained {len(self.manager._names_by_id)} finished job(s)",
        )

    def test_the_inflight_set_is_released(self):
        for _ in range(6):
            self.manager.submit("health_scan", _Instant(), operation_type="health_scan")
            _wait_for(
                lambda: self.manager.snapshot("health_scan").get("state") in
                {"done", "error", "cancelled", "partial"},
                timeout=5.0,
            )
        _wait_for(lambda: not self.manager._inflight, timeout=5.0)
        self.assertEqual(self.manager._inflight, set())


if __name__ == "__main__":
    unittest.main()
