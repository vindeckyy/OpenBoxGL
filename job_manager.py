"""Bounded, observable background work for the OpenBox backend."""

from __future__ import annotations

import inspect
import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from collections.abc import Callable


LOGGER = logging.getLogger("openbox.jobs")
MAX_ERROR_LENGTH = 800
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_JOBS = 128
MAX_JOB_NAME_LENGTH = 128
MAX_JOB_WORKERS = 16
MAX_JOB_RESULT_BYTES = 64 * 1024
MAX_BACKOFF_SECONDS = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_result(result):
    if not isinstance(result, dict):
        return {}
    try:
        size = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, OverflowError):
        return {}
    if size > MAX_JOB_RESULT_BYTES:
        return {"error": "Job result exceeded the size limit.", "result_truncated": True}
    return result


class JobManager:
    """Run bounded backend jobs and prevent stale workers overwriting new jobs."""

    def __init__(self, max_workers=DEFAULT_MAX_WORKERS, history_limit=50, max_jobs=DEFAULT_MAX_JOBS):
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._inflight: set[str] = set()
        self._history: list[dict] = []
        self._history_limit = max(0, min(int(history_limit), 200))
        self._max_jobs = max(1, min(int(max_jobs), 1024))
        workers = max(1, min(int(max_workers), MAX_JOB_WORKERS))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="openbox-job")
        self._observer = None

    def set_observer(self, observer):
        """Register a callback `observer(job)` invoked after a job finishes."""
        with self._lock:
            self._observer = observer

    def _notify(self, job):
        observer = self._observer
        if observer is not None:
            try:
                observer(job)
            except Exception:
                LOGGER.exception("Job observer failed")

    def snapshot(self, name):
        with self._lock:
            return dict(self._jobs.get(name, {}))

    def snapshots(self):
        with self._lock:
            return {name: dict(job) for name, job in self._jobs.items()}

    def history(self, limit=50):
        """Return finished jobs, newest first, so the UI can show a log."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            return [dict(job) for job in self._history[-limit:]][::-1]

    def _archive(self, job):
        if not self._history_limit:
            return
        with self._lock:
            self._history.append(dict(job))
            del self._history[: max(0, len(self._history) - self._history_limit)]

    def _prune_finished_locked(self):
        finished = {"done", "error", "cancelled"}
        for name, job in list(self._jobs.items()):
            if len(self._jobs) < self._max_jobs:
                return
            if job.get("state") in finished:
                self._jobs.pop(name, None)
                self._cancel_events.pop(job.get("job_id"), None)

    def submit(self, name, worker: Callable, *, replace=False, max_attempts=1, backoff_seconds=1.0):
        name = str(name).strip()
        if not name:
            raise ValueError("A job name is required.")
        if len(name) > MAX_JOB_NAME_LENGTH:
            raise ValueError("Job name is too long.")
        if any(ord(character) < 0x20 for character in name):
            raise ValueError("Job name contains control characters.")
        max_attempts = max(1, min(int(max_attempts), 5))
        try:
            backoff_seconds = float(backoff_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("Job backoff must be a number.") from error
        if not math.isfinite(backoff_seconds) or backoff_seconds < 0:
            raise ValueError("Job backoff must be finite and non-negative.")
        backoff_seconds = min(backoff_seconds, MAX_BACKOFF_SECONDS)
        with self._lock:
            current = self._jobs.get(name, {})
            if current.get("state") in {"queued", "running"} and not replace:
                return dict(current)
            if replace and current.get("job_id"):
                if current["job_id"] in self._inflight and len(self._inflight) >= self._max_jobs:
                    raise RuntimeError("The background job queue is full.")
                old_event = self._cancel_events.get(current["job_id"])
                if old_event:
                    old_event.set()
            self._jobs.pop(name, None)
            self._prune_finished_locked()
            if len(self._inflight) >= self._max_jobs:
                raise RuntimeError("The background job queue is full.")
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "name": name,
                "state": "queued",
                "started_at": "",
                "finished_at": "",
                "created_at": _now(),
                "error": "",
                "attempt": 0,
                "max_attempts": max_attempts,
                "duration_seconds": 0,
            }
            cancel_event = threading.Event()
            self._jobs[name] = job
            self._cancel_events[job_id] = cancel_event
            self._inflight.add(job_id)

        try:
            future = self._executor.submit(self._run_job, name, job_id, worker, max_attempts, backoff_seconds, cancel_event)
        except Exception:
            with self._lock:
                if self._jobs.get(name, {}).get("job_id") == job_id:
                    self._jobs.pop(name, None)
                self._cancel_events.pop(job_id, None)
                self._inflight.discard(job_id)
            raise
        with self._lock:
            self._futures[job_id] = future
            future.add_done_callback(self._drop_future)
            return dict(self._jobs[name])

    def _run_job(self, name, job_id, worker, max_attempts, backoff_seconds, cancel_event):
        """Execute one submitted job: retry loop, cancellation, and archiving."""
        accepts_context = False
        try:
            accepts_context = len(inspect.signature(worker).parameters) > 0
        except (TypeError, ValueError):
            pass
        started = time.monotonic()
        for attempt in range(1, max_attempts + 1):
            with self._lock:
                current = self._jobs.get(name)
                if not current or current.get("job_id") != job_id:
                    self._inflight.discard(job_id)
                    self._cancel_events.pop(job_id, None)
                    return
                if cancel_event.is_set():
                    notification = None
                    if current.get("state") != "cancelled":
                        current.update({"state": "cancelled", "finished_at": _now()})
                        self._archive(current)
                        notification = dict(current)
                    self._cancel_events.pop(job_id, None)
                    self._inflight.discard(job_id)
                    cancelled = True
                else:
                    cancelled = False
                    current.update({"state": "running", "started_at": current.get("started_at") or _now(), "attempt": attempt})
            if cancelled:
                if notification is not None:
                    self._notify(notification)
                return
            try:
                result = worker(cancel_event) if accepts_context else worker()
                if cancel_event.is_set():
                    state = "cancelled"
                    result = {}
                else:
                    state = "done"
                notification = None
                with self._lock:
                    current = self._jobs.get(name)
                    if current and current.get("job_id") == job_id:
                        current.update(_bounded_result(result))
                        current.update({
                            "state": state,
                            "finished_at": _now(),
                            "duration_seconds": round(time.monotonic() - started, 3),
                        })
                        self._cancel_events.pop(job_id, None)
                        self._inflight.discard(job_id)
                        self._archive(current)
                        notification = dict(current)
                if notification is not None:
                    self._notify(notification)
                return
            except Exception as error:  # worker isolation boundary
                LOGGER.exception("Backend job %s failed on attempt %s", name, attempt)
                if attempt < max_attempts and not cancel_event.is_set():
                    delay = min(MAX_BACKOFF_SECONDS, backoff_seconds * (2 ** (attempt - 1)))
                    cancel_event.wait(delay)
                    continue
                notification = None
                with self._lock:
                    current = self._jobs.get(name)
                    if current and current.get("job_id") == job_id:
                        current.update({
                            "state": "cancelled" if cancel_event.is_set() else "error",
                            "error": str(error)[:MAX_ERROR_LENGTH],
                            "finished_at": _now(),
                            "duration_seconds": round(time.monotonic() - started, 3),
                        })
                        self._cancel_events.pop(job_id, None)
                        self._inflight.discard(job_id)
                        self._archive(current)
                        notification = dict(current)
                if notification is not None:
                    self._notify(notification)
                return

    def _drop_future(self, future):
        """Forget a finished future so completed jobs cannot accumulate."""
        with self._lock:
            for job_id, stored in list(self._futures.items()):
                if stored is future:
                    self._futures.pop(job_id, None)
                    break

    def cancel(self, name):
        with self._lock:
            job = self._jobs.get(name)
            if not job or job.get("state") not in {"queued", "running"}:
                return False
            event = self._cancel_events.get(job.get("job_id"))
            if event:
                event.set()
            if job.get("state") == "queued":
                job.update({"state": "cancelled", "finished_at": _now()})
                future = self._futures.get(job.get("job_id"))
                if future is not None and future.cancel():
                    self._futures.pop(job.get("job_id"), None)
                    self._inflight.discard(job.get("job_id"))
                self._cancel_events.pop(job.get("job_id"), None)
                self._archive(job)
            return True

    def shutdown(self, wait=True, cancel_futures=False):
        notifications = []
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
            if cancel_futures:
                for _name, job in list(self._jobs.items()):
                    if job.get("state") != "queued":
                        continue
                    job_id = job.get("job_id")
                    future = self._futures.get(job_id)
                    if future is not None and not future.cancel():
                        continue
                    job.update({"state": "cancelled", "finished_at": _now()})
                    self._archive(job)
                    notifications.append(dict(job))
                    self._cancel_events.pop(job_id, None)
                    self._futures.pop(job_id, None)
                    self._inflight.discard(job_id)
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        for job in notifications:
            self._notify(job)
