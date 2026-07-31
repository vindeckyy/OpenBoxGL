"""Bounded, observable background work for the OpenBox backend."""

from __future__ import annotations

import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable


LOGGER = logging.getLogger("openbox.jobs")
MAX_ERROR_LENGTH = 800


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    """Run bounded backend jobs and prevent stale workers overwriting new jobs."""

    def __init__(self, max_workers=4):
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="openbox-job")

    def snapshot(self, name):
        with self._lock:
            return dict(self._jobs.get(name, {}))

    def snapshots(self):
        with self._lock:
            return {name: dict(job) for name, job in self._jobs.items()}

    def submit(self, name, worker: Callable, *, replace=False, max_attempts=1, backoff_seconds=1.0):
        name = str(name).strip()
        if not name:
            raise ValueError("A job name is required.")
        max_attempts = max(1, min(int(max_attempts), 5))
        with self._lock:
            current = self._jobs.get(name, {})
            if current.get("state") in {"queued", "running"} and not replace:
                return dict(current)
            if replace and current.get("job_id"):
                old_event = self._cancel_events.get(current["job_id"])
                if old_event:
                    old_event.set()
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

        def run():
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
                        return
                    if cancel_event.is_set():
                        current.update({"state": "cancelled", "finished_at": _now()})
                        return
                    current.update({"state": "running", "started_at": current.get("started_at") or _now(), "attempt": attempt})
                try:
                    result = worker(cancel_event) if accepts_context else worker()
                    if cancel_event.is_set():
                        state = "cancelled"
                        result = {}
                    else:
                        state = "done"
                    with self._lock:
                        current = self._jobs.get(name)
                        if current and current.get("job_id") == job_id:
                            if isinstance(result, dict):
                                current.update(result)
                            current.update({
                                "state": state,
                                "finished_at": _now(),
                                "duration_seconds": round(time.monotonic() - started, 3),
                            })
                    return
                except Exception as error:  # worker isolation boundary
                    LOGGER.exception("Backend job %s failed on attempt %s", name, attempt)
                    if attempt < max_attempts and not cancel_event.is_set():
                        cancel_event.wait(max(0.0, float(backoff_seconds)) * (2 ** (attempt - 1)))
                        continue
                    with self._lock:
                        current = self._jobs.get(name)
                        if current and current.get("job_id") == job_id:
                            current.update({
                                "state": "cancelled" if cancel_event.is_set() else "error",
                                "error": str(error)[:MAX_ERROR_LENGTH],
                                "finished_at": _now(),
                                "duration_seconds": round(time.monotonic() - started, 3),
                            })
                    return

        future = self._executor.submit(run)
        with self._lock:
            self._futures[job_id] = future
            return dict(self._jobs[name])

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
            return True

    def shutdown(self, wait=True, cancel_futures=False):
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
