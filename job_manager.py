"""Bounded, observable background work for the OpenBox backend."""

from __future__ import annotations

import inspect
import json
import logging
import math
import threading
import time
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

OPERATION_TYPE_BY_NAME = {
    "emulator-install-all": "emulator.install",
    "emulator-update-all": "emulator.update",
    "media-bulk": "media.bulk_download",
    "metadata": "metadata.db_sync",
    "metadata-match": "metadata.apply",
    "library-backup": "library.backup",
    "library-restore": "library.restore",
    "cloud-sync": "cloud.sync",
    "updater-install": "updater.install",
    "saves-scan": "saves.scan",
    "saves-backup": "saves.backup",
    "saves-restore": "saves.restore",
}


def operation_type_for_name(name: str) -> str:
    if name in OPERATION_TYPE_BY_NAME:
        return OPERATION_TYPE_BY_NAME[name]
    if name.startswith("emulator-install:"):
        return "emulator.install"
    if name.startswith("update:"):
        return "emulator.update"
    if name.startswith("gameyfin:"):
        return "gameyfin.install"
    if name.startswith("saves-backup:"):
        return "saves.backup"
    if name.startswith("saves-restore:"):
        return "saves.restore"
    if name.startswith("setup-scan:"):
        return "setup.scan"
    if name.startswith("setup-revalidate:"):
        return "setup.revalidate"
    if name.startswith("setup-commit:"):
        return "setup.commit"
    if name.startswith("metadata-match-preview:"):
        return "metadata.match_preview"
    if name.startswith("metadata-match-apply:"):
        return "metadata.apply"
    return "setup.scan"


def operation_title_for_name(name: str) -> str:
    if name == "emulator-install-all":
        return "Install all emulators"
    if name == "emulator-update-all":
        return "Update all emulators"
    if name.startswith("emulator-install:"):
        return f"Install emulator {name.split(':', 1)[1]}"
    if name.startswith("update:"):
        return f"Update emulator {name.split(':', 1)[1]}"
    if name.startswith("gameyfin:"):
        return f"Install Gameyfin game {name.split(':', 1)[1]}"
    if name == "media-bulk":
        return "Bulk media download"
    if name == "metadata":
        return "Metadata database sync"
    if name == "metadata-match":
        return "Metadata auto-match"
    if name == "library-backup":
        return "Library backup"
    if name == "library-restore":
        return "Library restore"
    if name == "cloud-sync":
        return "Cloud sync"
    if name == "updater-install":
        return "Install update"
    if name == "saves-scan":
        return "Save path scan"
    if name == "saves-backup":
        return "Save backup"
    if name == "saves-restore":
        return "Save restore"
    if name.startswith("saves-backup:"):
        return "Save backup"
    if name.startswith("saves-restore:"):
        return "Save restore"
    return name.replace("-", " ").replace(":", " ").strip().title() or "Background job"


def _get_operation_service():
    from pkg.state.operations import get_operation_service
    return get_operation_service()


def legacy_snapshot_fields(job: dict, operation: dict | None = None) -> dict:
    """Build additive legacy /api/jobs snapshot fields from job + operation."""
    snapshot = dict(job)
    op = operation or {}
    for key in (
        "type",
        "phase",
        "current",
        "total",
        "message",
        "can_cancel",
        "can_retry",
        "can_resume",
        "result",
    ):
        value = op.get(key)
        if value is not None:
            snapshot[key] = value
    for key in ("job_id", "started_at", "finished_at"):
        if op.get(key):
            snapshot[key] = op[key]
    op_state = op.get("state")
    job_state = str(snapshot.get("state") or "")
    if op_state and not (job_state in {"done", "error", "cancelled"} and op_state == "cancelling"):
        snapshot["state"] = op_state
    error = op.get("error")
    if error:
        snapshot["error"] = error if isinstance(error, dict) else str(error)
    elif snapshot.get("error") and not isinstance(snapshot.get("error"), dict):
        snapshot["error"] = {"code": "INTERNAL_ERROR", "message": str(snapshot["error"]), "request_id": ""}
    return snapshot


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


class JobContext(threading.Event):
    """Execution context provided to background workers, supporting cancellation and progress updates."""

    def __init__(self, manager: JobManager, job_name: str, job_id: str):
        super().__init__()
        self._manager = manager
        self._job_name = job_name
        self._job_id = job_id

    @property
    def job_name(self) -> str:
        return self._job_name

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def manager(self) -> JobManager:
        return self._manager

    def is_cancelled(self) -> bool:
        return self.is_set()

    def progress(self, **kwargs) -> dict:
        """Report incremental progress for this job."""
        return self._manager.update_progress(self._job_name, **kwargs)


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
        self._progress_observer = None
        self._names_by_id: dict[str, str] = {}

    def get_operation_id(self, name: str) -> str | None:
        with self._lock:
            job = self._jobs.get(name)
            if job and job.get("job_id"):
                return str(job["job_id"])
        return None

    def name_for_operation_id(self, job_id: str) -> str | None:
        with self._lock:
            return self._names_by_id.get(str(job_id))

    def cancel_by_id(self, job_id: str) -> bool:
        name = self.name_for_operation_id(job_id)
        if not name:
            return False
        return self.cancel(name)

    def _operation_for_job(self, job_id: str):
        try:
            return _get_operation_service().get(job_id)
        except Exception:
            LOGGER.exception("Failed to load operation %s", job_id)
            return None

    def _create_operation(self, name: str, *, input_data: dict | None = None) -> dict:
        service = _get_operation_service()
        return service.create(
            operation_type=operation_type_for_name(name),
            title=operation_title_for_name(name),
            input_data=input_data or {},
        )

    def _sync_operation_progress(self, job_id: str, **kwargs) -> None:
        if not job_id:
            return
        service = _get_operation_service()
        progress = {}
        for key in ("phase", "current", "total", "message", "checkpoint"):
            if key in kwargs and kwargs[key] is not None:
                progress[key] = kwargs[key]
        if not progress:
            return
        try:
            service.update_progress(job_id, **progress)
        except Exception:
            LOGGER.exception("Failed to sync operation progress for %s", job_id)

    def _sync_operation_running(self, job_id: str) -> None:
        if not job_id:
            return
        try:
            _get_operation_service().mark_running(job_id)
        except Exception:
            LOGGER.exception("Failed to mark operation running for %s", job_id)

    def _sync_operation_finish(self, job_id: str, job: dict) -> None:
        if not job_id:
            return
        state = str(job.get("state") or "done")
        op_state = state
        if state == "done":
            errors = job.get("errors")
            if isinstance(errors, list) and errors:
                op_state = "partial"
        result = None
        for key in ("added", "merged", "skipped", "excluded", "failed", "completed", "downloaded", "updated"):
            if key in job:
                result = result or {}
                try:
                    result[key] = int(job[key])
                except (TypeError, ValueError):
                    pass
        error = None
        if op_state == "error":
            raw = job.get("error")
            error = raw if isinstance(raw, dict) else {"code": "INTERNAL_ERROR", "message": str(raw or ""), "request_id": ""}
        try:
            _get_operation_service().finish(
                job_id,
                state=op_state,
                result=result,
                error=error,
                message=str(job.get("message") or ""),
            )
        except Exception:
            LOGGER.exception("Failed to finish operation %s", job_id)

    def set_observer(self, observer):
        """Register a callback `observer(job)` invoked after a job finishes."""
        with self._lock:
            self._observer = observer

    def set_progress_observer(self, observer):
        """Register a callback `observer(job)` invoked when a job reports progress."""
        with self._lock:
            self._progress_observer = observer

    def update_progress(self, name, **kwargs):
        """Update in-flight job progress metrics and notify the progress observer."""
        notification = None
        job_id = None
        with self._lock:
            current = self._jobs.get(name)
            if not current:
                for job in self._jobs.values():
                    if job.get("job_id") == name:
                        current = job
                        break
            if not current or current.get("state") not in {"queued", "running"}:
                return {}
            current.update(kwargs)
            job_id = current.get("job_id")
            notification = dict(current)
        if job_id:
            self._sync_operation_progress(job_id, **kwargs)
        if notification is not None:
            observer = self._progress_observer
            if observer is not None:
                try:
                    observer(notification)
                except Exception:
                    LOGGER.exception("Job progress observer failed")
        return notification or {}

    def _notify(self, job):
        try:
            self._sync_operation_finish(job.get("job_id"), job)
        except Exception:
            LOGGER.exception("Failed to sync terminal operation state for %s", job.get("job_id"))
        observer = self._observer
        if observer is not None:
            try:
                observer(job)
            except Exception:
                LOGGER.exception("Job observer failed")

    def snapshot(self, name):
        with self._lock:
            job = dict(self._jobs.get(name, {}))
        if job.get("job_id"):
            operation = self._operation_for_job(job["job_id"])
            return legacy_snapshot_fields(job, operation)
        return job

    def snapshots(self):
        with self._lock:
            names = list(self._jobs.keys())
        return {name: self.snapshot(name) for name in names}

    def history(self, limit=50):
        """Return finished jobs, newest first, so the UI can show a log."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            items = [dict(job) for job in self._history[-limit:]][::-1]
        enriched = []
        for job in items:
            operation = self._operation_for_job(job.get("job_id", "")) if job.get("job_id") else None
            enriched.append(legacy_snapshot_fields(job, operation))
        return enriched

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

    def submit(
        self,
        name,
        worker: Callable,
        *,
        replace=False,
        max_attempts=1,
        backoff_seconds=1.0,
        operation_type: str | None = None,
        input_data: dict | None = None,
    ):
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
            if operation_type or input_data:
                service = _get_operation_service()
                operation = service.create(
                    operation_type=operation_type or operation_type_for_name(name),
                    title=operation_title_for_name(name),
                    input_data=input_data or {},
                )
            else:
                operation = self._create_operation(name)
            job_id = operation["job_id"]
            job = {
                "job_id": job_id,
                "name": name,
                "state": "queued",
                "type": operation.get("type", operation_type_for_name(name)),
                "started_at": "",
                "finished_at": "",
                "created_at": _now(),
                "error": "",
                "attempt": 0,
                "max_attempts": max_attempts,
                "duration_seconds": 0,
                "can_cancel": operation.get("can_cancel", True),
                "can_retry": operation.get("can_retry", False),
                "can_resume": operation.get("can_resume", False),
            }
            cancel_event = JobContext(self, name, job_id)
            self._jobs[name] = job
            self._names_by_id[job_id] = name
            self._cancel_events[job_id] = cancel_event
            self._inflight.add(job_id)

        try:
            future = self._executor.submit(self._run_job, name, job_id, worker, max_attempts, backoff_seconds, cancel_event)
        except Exception:
            with self._lock:
                if self._jobs.get(name, {}).get("job_id") == job_id:
                    self._jobs.pop(name, None)
                self._cancel_events.pop(job_id, None)
                self._names_by_id.pop(job_id, None)
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
        try:
            for attempt in range(1, max_attempts + 1):
                with self._lock:
                    current = self._jobs.get(name)
                    if not current or current.get("job_id") != job_id:
                        return
                    if cancel_event.is_set():
                        notification = None
                        if current.get("state") != "cancelled":
                            current.update({"state": "cancelled", "finished_at": _now()})
                            self._archive(current)
                            notification = dict(current)
                        cancelled = True
                    else:
                        cancelled = False
                        current.update({"state": "running", "started_at": current.get("started_at") or _now(), "attempt": attempt})
                if not cancelled and attempt == 1:
                    self._sync_operation_running(job_id)
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
                            self._archive(current)
                            notification = dict(current)
                    if notification is not None:
                        self._notify(notification)
                    return
        finally:
            with self._lock:
                self._inflight.discard(job_id)
                self._cancel_events.pop(job_id, None)
                self._futures.pop(job_id, None)

    def _drop_future(self, future):
        """Forget a finished future so completed jobs cannot accumulate."""
        with self._lock:
            for job_id, stored in list(self._futures.items()):
                if stored is future:
                    self._futures.pop(job_id, None)
                    break

    def cancel(self, name):
        notification = None
        with self._lock:
            job = self._jobs.get(name)
            if not job or job.get("state") not in {"queued", "running"}:
                return False
            job_id = job.get("job_id")
            event = self._cancel_events.get(job_id)
            if event:
                event.set()
            if job.get("state") == "queued":
                job.update({"state": "cancelled", "finished_at": _now()})
                future = self._futures.get(job_id)
                if future is not None and future.cancel():
                    self._futures.pop(job_id, None)
                    self._inflight.discard(job_id)
                self._cancel_events.pop(job_id, None)
                self._archive(job)
                notification = dict(job)
            else:
                try:
                    _get_operation_service().request_cancel(str(job_id))
                except Exception:
                    LOGGER.exception("Failed to request operation cancel for %s", job_id)
        if notification is not None:
            self._notify(notification)
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
