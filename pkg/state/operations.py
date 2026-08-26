"""Authoritative durable operation service for long-running backend work."""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend_io import atomic_write_text
from crash_report import tokenize_home_paths
from openbox import DATA
from pkg.parity.parity_redact import REDACTED_KEYS

LOGGER = logging.getLogger("openbox.operations")

OPERATIONS_VERSION = 1
MAX_OPERATIONS = 100
RETENTION_DAYS = 30
MAX_PROGRESS_PERSIST_INTERVAL = 0.5
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
MAX_ITEMS_LIST_LIMIT = 200

ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
TERMINAL_STATES = frozenset({"done", "partial", "error", "cancelled", "interrupted"})

SSE_EVENT_QUEUED = "job.queued"
SSE_EVENT_PROGRESS = "job.progress"
SSE_EVENT_CANCELLING = "job.cancelling"
SSE_EVENT_FINISHED = "job.finished"
SSE_EVENT_INTERRUPTED = "job.interrupted"

SSE_PAYLOAD_KEYS = {
    SSE_EVENT_QUEUED: ("job_id", "type", "title", "state"),
    SSE_EVENT_PROGRESS: ("job_id", "state", "phase", "current", "total", "message"),
    SSE_EVENT_CANCELLING: ("job_id", "state", "message"),
    SSE_EVENT_FINISHED: ("job_id", "state", "result", "error"),
    SSE_EVENT_INTERRUPTED: ("job_id", "state", "message"),
}

OPERATION_DOCUMENT_KEYS = (
    "job_id",
    "root_job_id",
    "retry_of",
    "resume_of",
    "type",
    "title",
    "state",
    "phase",
    "current",
    "total",
    "message",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "can_cancel",
    "can_retry",
    "can_resume",
    "input",
    "checkpoint",
    "result",
    "error",
)

# resume: checkpoint-safe | retry_only | retry_failed_only
OPERATION_POLICIES: dict[str, dict] = {
    "setup.scan": {"resume": "checkpoint", "retry": True, "refuse_cancel_promote": False},
    "setup.revalidate": {"resume": "checkpoint", "retry": True, "refuse_cancel_promote": False},
    "setup.commit": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "metadata.db_sync": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "metadata.match_preview": {"resume": "checkpoint", "retry": True, "refuse_cancel_promote": False},
    "metadata.apply": {"resume": "checkpoint", "retry": True, "refuse_cancel_promote": True},
    "media.bulk_download": {"resume": "checkpoint", "retry": "failed_only", "refuse_cancel_promote": False},
    "media.cleanup": {"resume": "checkpoint", "retry": True, "refuse_cancel_promote": False},
    "emulator.install": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "emulator.update": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "gameyfin.install": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "saves.scan": {"resume": "checkpoint", "retry": True, "refuse_cancel_promote": False},
    "saves.backup": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "library.backup": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "library.restore": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "cloud.sync": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
    "updater.install": {"resume": "retry_only", "retry": True, "refuse_cancel_promote": True},
}


class JobStateConflictError(Exception):
    code = "JOB_STATE_CONFLICT"


class JobNotCancellableError(Exception):
    code = "JOB_NOT_CANCELLABLE"


class JobNotResumableError(Exception):
    code = "JOB_NOT_RESUMABLE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def operations_path(data_path: Path | None = None) -> Path:
    base = (data_path or DATA).parent
    return base / "operations.json"


def operation_items_path(job_id: str, data_path: Path | None = None) -> Path:
    base = (data_path or DATA).parent / "operations_items"
    return base / f"{job_id}.json"


def sanitize_operation_input(value) -> dict:
    """Remove secrets and tokenize absolute home paths from operation input."""
    if not isinstance(value, dict):
        return {}
    cleaned: dict = {}
    for key, item in value.items():
        if key in REDACTED_KEYS:
            continue
        cleaned[key] = tokenize_home_paths(item)
    return cleaned


def _policy(operation_type: str) -> dict:
    return OPERATION_POLICIES.get(
        operation_type,
        {"resume": "retry_only", "retry": True, "refuse_cancel_promote": False},
    )


def _normalize_error(error) -> dict | None:
    if not error:
        return None
    if isinstance(error, dict):
        code = str(error.get("code") or "INTERNAL_ERROR")
        message = str(error.get("message") or "")
        request_id = str(error.get("request_id") or "")
        return {"code": code, "message": message, "request_id": request_id}
    return {"code": "INTERNAL_ERROR", "message": str(error), "request_id": ""}


def _normalize_result(result) -> dict | None:
    if not isinstance(result, dict):
        return None
    allowed = ("added", "merged", "skipped", "excluded", "failed", "completed", "downloaded")
    cleaned = {key: int(result[key]) for key in allowed if key in result}
    return cleaned or None


class OperationService:
    """Persist and manage durable operations separate from library.json."""

    def __init__(self, data_path: Path | None = None):
        self._data_path = data_path or DATA
        self._path = operations_path(self._data_path)
        self._lock = threading.RLock()
        self._operations: dict[str, dict] = {}
        self._order: list[str] = []
        self._promoting: set[str] = set()
        self._last_persist: dict[str, float] = {}
        self._last_sse: dict[str, float] = {}
        self._event_observers: list = []
        self._load()

    def set_event_observer(self, observer) -> None:
        with self._lock:
            self._event_observers.append(observer)

    def _emit_sse(self, event_name: str, payload: dict) -> None:
        required = SSE_PAYLOAD_KEYS.get(event_name)
        if required:
            payload = {key: payload.get(key) for key in required}
        observers = list(self._event_observers)
        for observer in observers:
            try:
                observer(event_name, payload)
            except Exception:
                LOGGER.exception("Operation SSE observer failed for %s", event_name)

    def _snapshot(self, record: dict) -> dict:
        doc = {
            "job_id": str(record.get("job_id") or ""),
            "root_job_id": str(record.get("root_job_id") or record.get("job_id") or ""),
            "retry_of": record.get("retry_of"),
            "resume_of": record.get("resume_of"),
            "type": str(record.get("type") or ""),
            "title": str(record.get("title") or ""),
            "state": str(record.get("state") or "queued"),
            "phase": record.get("phase"),
            "current": int(record.get("current") or 0),
            "total": record.get("total"),
            "message": str(record.get("message") or ""),
            "created_at": str(record.get("created_at") or _now()),
            "updated_at": str(record.get("updated_at") or _now()),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "can_cancel": bool(record.get("can_cancel")),
            "can_retry": bool(record.get("can_retry")),
            "can_resume": bool(record.get("can_resume")),
            "input": sanitize_operation_input(record.get("input") or {}),
            "checkpoint": copy.deepcopy(record.get("checkpoint")),
            "result": _normalize_result(record.get("result")),
            "error": _normalize_error(record.get("error")),
        }
        if doc["total"] is not None:
            doc["total"] = int(doc["total"])
        self._refresh_capabilities(doc)
        return doc

    def _refresh_capabilities(self, doc: dict) -> None:
        policy = _policy(doc["type"])
        state = doc["state"]
        promoting = doc["job_id"] in self._promoting
        checkpoint = doc.get("checkpoint")
        resume_mode = policy.get("resume", "retry_only")

        can_retry = bool(policy.get("retry")) and state in TERMINAL_STATES
        can_resume = False
        if resume_mode == "checkpoint" and state == "interrupted" and isinstance(checkpoint, dict) and checkpoint:
            can_resume = True
        if resume_mode in {"retry_only", "failed_only"}:
            can_resume = False

        can_cancel = state in ACTIVE_STATES
        if promoting and policy.get("refuse_cancel_promote"):
            can_cancel = False

        doc["can_cancel"] = can_cancel
        doc["can_retry"] = can_retry
        doc["can_resume"] = can_resume

    def _load(self) -> None:
        path = self._path
        payload = {"version": OPERATIONS_VERSION, "operations": []}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("operations"), list):
                    payload = raw
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                LOGGER.warning("Discarding corrupt operations store at %s", path)
        operations = []
        for entry in payload.get("operations", []):
            if not isinstance(entry, dict) or not entry.get("job_id"):
                continue
            operations.append(entry)
        with self._lock:
            self._operations.clear()
            self._order.clear()
            for entry in operations:
                job_id = str(entry["job_id"])
                if entry.get("state") in ACTIVE_STATES:
                    entry = dict(entry)
                    entry["state"] = "interrupted"
                    entry["message"] = str(entry.get("message") or "Interrupted by shutdown.")
                    entry["updated_at"] = _now()
                    if not entry.get("finished_at"):
                        entry["finished_at"] = entry["updated_at"]
                snapshot = self._snapshot(entry)
                self._operations[job_id] = snapshot
                self._order.append(job_id)
            self._order.sort(key=lambda item: self._operations[item]["updated_at"], reverse=True)

    def _prune_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        finished_states = TERMINAL_STATES
        kept: list[str] = []
        for job_id in self._order:
            doc = self._operations.get(job_id)
            if not doc:
                continue
            finished_at = doc.get("finished_at")
            if doc["state"] in finished_states and finished_at:
                try:
                    finished = datetime.fromisoformat(str(finished_at))
                except ValueError:
                    finished = None
                if finished is not None and finished < cutoff:
                    self._operations.pop(job_id, None)
                    continue
            kept.append(job_id)
        self._order = kept[:MAX_OPERATIONS]

    def persist(self) -> bool:
        with self._lock:
            self._prune_locked()
            payload = {
                "version": OPERATIONS_VERSION,
                "operations": [self._operations[job_id] for job_id in self._order if job_id in self._operations],
            }
            data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        try:
            atomic_write_text(self._path, data, mode=0o600)
            return True
        except OSError:
            LOGGER.exception("Failed to persist operations store")
            return False

    def _should_persist_progress(self, job_id: str, *, phase_changed: bool) -> bool:
        if phase_changed:
            return True
        now = time.monotonic()
        last = self._last_persist.get(job_id, 0.0)
        if now - last >= MAX_PROGRESS_PERSIST_INTERVAL:
            self._last_persist[job_id] = now
            return True
        return False

    def _should_emit_progress(self, job_id: str) -> bool:
        now = time.monotonic()
        last = self._last_sse.get(job_id, 0.0)
        if now - last >= MAX_PROGRESS_PERSIST_INTERVAL:
            self._last_sse[job_id] = now
            return True
        return False

    def create(
        self,
        *,
        operation_type: str,
        title: str,
        input_data: dict | None = None,
        retry_of: str | None = None,
        resume_of: str | None = None,
        root_job_id: str | None = None,
        checkpoint: dict | None = None,
        message: str = "",
    ) -> dict:
        job_id = uuid.uuid4().hex
        now = _now()
        record = {
            "job_id": job_id,
            "root_job_id": root_job_id or job_id,
            "retry_of": retry_of,
            "resume_of": resume_of,
            "type": operation_type,
            "title": title,
            "state": "queued",
            "phase": None,
            "current": 0,
            "total": None,
            "message": message,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "input": sanitize_operation_input(input_data or {}),
            "checkpoint": copy.deepcopy(checkpoint),
            "result": None,
            "error": None,
        }
        with self._lock:
            snapshot = self._snapshot(record)
            self._operations[job_id] = snapshot
            self._order.insert(0, job_id)
            self._prune_locked()
        self.persist()
        self._emit_sse(
            SSE_EVENT_QUEUED,
            {
                "job_id": snapshot["job_id"],
                "type": snapshot["type"],
                "title": snapshot["title"],
                "state": snapshot["state"],
            },
        )
        return dict(snapshot)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._operations.get(job_id)
            return dict(record) if record else None

    def list_jobs(self, *, cursor: str | None = None, limit: int = DEFAULT_LIST_LIMIT) -> dict:
        limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
        with self._lock:
            ordered = [self._operations[job_id] for job_id in self._order if job_id in self._operations]
        start = 0
        if cursor:
            for index, doc in enumerate(ordered):
                if doc["job_id"] == cursor:
                    start = index + 1
                    break
        page = ordered[start : start + limit]
        next_cursor = page[-1]["job_id"] if len(page) == limit and start + limit < len(ordered) else None
        return {
            "cursor": cursor,
            "next_cursor": next_cursor,
            "jobs": [dict(doc) for doc in page],
        }

    def mark_running(self, job_id: str, *, phase: str | None = None, message: str = "") -> dict:
        with self._lock:
            record = self._operations.get(job_id)
            if not record or record["state"] not in {"queued", "running"}:
                raise JobStateConflictError(f"Operation {job_id} is not runnable.")
            record = dict(record)
            record["state"] = "running"
            record["started_at"] = record.get("started_at") or _now()
            record["updated_at"] = _now()
            if phase is not None:
                record["phase"] = phase
            if message:
                record["message"] = message
            snapshot = self._snapshot(record)
            self._operations[job_id] = snapshot
        self.persist()
        return dict(snapshot)

    def update_progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        checkpoint: dict | None = None,
        persist: bool = True,
    ) -> dict:
        phase_changed = phase is not None
        with self._lock:
            record = self._operations.get(job_id)
            if not record or record["state"] not in ACTIVE_STATES:
                raise JobStateConflictError(f"Operation {job_id} is not active.")
            record = dict(record)
            if phase is not None:
                record["phase"] = phase
            if current is not None:
                record["current"] = int(current)
            if total is not None:
                record["total"] = int(total)
            if message is not None:
                record["message"] = message
            if checkpoint is not None:
                record["checkpoint"] = copy.deepcopy(checkpoint)
            record["updated_at"] = _now()
            snapshot = self._snapshot(record)
            self._operations[job_id] = snapshot
            should_persist = persist and self._should_persist_progress(job_id, phase_changed=phase_changed)
            should_emit = self._should_emit_progress(job_id)
        if should_persist:
            self.persist()
        if should_emit:
            self._emit_sse(
                SSE_EVENT_PROGRESS,
                {
                    "job_id": snapshot["job_id"],
                    "state": snapshot["state"],
                    "phase": snapshot["phase"],
                    "current": snapshot["current"],
                    "total": snapshot["total"],
                    "message": snapshot["message"],
                },
            )
        return dict(snapshot)

    def set_promote_phase(self, job_id: str, *, active: bool) -> None:
        with self._lock:
            if active:
                self._promoting.add(job_id)
            else:
                self._promoting.discard(job_id)
            record = self._operations.get(job_id)
            if record:
                self._operations[job_id] = self._snapshot(record)

    def request_cancel(self, job_id: str, *, message: str = "Cancellation requested.") -> dict:
        with self._lock:
            record = self._operations.get(job_id)
            if not record:
                raise JobStateConflictError(f"Operation {job_id} was not found.")
            snapshot = self._snapshot(record)
            if not snapshot["can_cancel"]:
                raise JobNotCancellableError(f"Operation {job_id} cannot be cancelled.")
            if record["state"] not in ACTIVE_STATES:
                raise JobStateConflictError(f"Operation {job_id} is not active.")
            record = dict(record)
            record["state"] = "cancelling"
            record["message"] = message
            record["updated_at"] = _now()
            snapshot = self._snapshot(record)
            self._operations[job_id] = snapshot
        self.persist()
        self._emit_sse(
            SSE_EVENT_CANCELLING,
            {"job_id": snapshot["job_id"], "state": snapshot["state"], "message": snapshot["message"]},
        )
        return dict(snapshot)

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        result: dict | None = None,
        error: dict | str | None = None,
        message: str | None = None,
        checkpoint: dict | None = None,
    ) -> dict:
        if state not in TERMINAL_STATES:
            raise ValueError(f"Invalid terminal state: {state}")
        with self._lock:
            record = self._operations.get(job_id)
            if not record:
                raise JobStateConflictError(f"Operation {job_id} was not found.")
            record = dict(record)
            record["state"] = state
            record["updated_at"] = _now()
            record["finished_at"] = record.get("finished_at") or _now()
            if result is not None:
                record["result"] = _normalize_result(result)
            if error is not None:
                record["error"] = _normalize_error(error)
            if message is not None:
                record["message"] = message
            if checkpoint is not None:
                record["checkpoint"] = copy.deepcopy(checkpoint)
            self._promoting.discard(job_id)
            snapshot = self._snapshot(record)
            self._operations[job_id] = snapshot
        self.persist()
        event = SSE_EVENT_INTERRUPTED if state == "interrupted" else SSE_EVENT_FINISHED
        if event == SSE_EVENT_INTERRUPTED:
            self._emit_sse(
                event,
                {"job_id": snapshot["job_id"], "state": snapshot["state"], "message": snapshot["message"]},
            )
        else:
            self._emit_sse(
                event,
                {
                    "job_id": snapshot["job_id"],
                    "state": snapshot["state"],
                    "result": snapshot["result"],
                    "error": snapshot["error"],
                },
            )
        return dict(snapshot)

    def retry(self, job_id: str) -> dict:
        with self._lock:
            record = self._operations.get(job_id)
            if not record:
                raise JobStateConflictError(f"Operation {job_id} was not found.")
            snapshot = self._snapshot(record)
            if not snapshot["can_retry"]:
                raise JobStateConflictError(f"Operation {job_id} cannot be retried.")
            policy = _policy(snapshot["type"])
            input_data = copy.deepcopy(snapshot["input"])
            if policy.get("retry") == "failed_only":
                failed_ids = []
                checkpoint = snapshot.get("checkpoint") or {}
                if isinstance(checkpoint, dict):
                    failed_ids = list(checkpoint.get("failed_game_ids") or [])
                input_data = {"failed_game_ids": failed_ids}
        return self.create(
            operation_type=snapshot["type"],
            title=snapshot["title"],
            input_data=input_data,
            retry_of=snapshot["job_id"],
            root_job_id=snapshot["root_job_id"],
        )

    def resume(self, job_id: str) -> dict:
        with self._lock:
            record = self._operations.get(job_id)
            if not record:
                raise JobStateConflictError(f"Operation {job_id} was not found.")
            snapshot = self._snapshot(record)
            if not snapshot["can_resume"]:
                raise JobNotResumableError(f"Operation {job_id} cannot be resumed.")
            policy = _policy(snapshot["type"])
            if policy.get("resume") != "checkpoint":
                raise JobNotResumableError(f"Operation {job_id} cannot be resumed.")
        return self.create(
            operation_type=snapshot["type"],
            title=snapshot["title"],
            input_data=copy.deepcopy(snapshot["input"]),
            resume_of=snapshot["job_id"],
            root_job_id=snapshot["root_job_id"],
            checkpoint=copy.deepcopy(snapshot.get("checkpoint")),
            message="Resuming from checkpoint.",
        )

    def add_item_failure(
        self,
        job_id: str,
        *,
        item_id: str,
        label: str,
        state: str,
        error: dict | str | None = None,
    ) -> dict:
        path = operation_items_path(job_id, self._data_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"job_id": job_id, "items": []}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
                    payload = loaded
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                payload = {"job_id": job_id, "items": []}
        entry = {
            "item_id": str(item_id),
            "label": str(label),
            "state": str(state),
            "error": _normalize_error(error),
        }
        payload["items"] = [item for item in payload["items"] if item.get("item_id") != entry["item_id"]]
        payload["items"].append(entry)
        try:
            atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", mode=0o600)
        except OSError:
            LOGGER.exception("Failed to persist operation items for %s", job_id)
        return entry

    def list_items(self, job_id: str, *, cursor: str | None = None, limit: int = DEFAULT_LIST_LIMIT) -> dict:
        limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_ITEMS_LIST_LIMIT))
        path = operation_items_path(job_id, self._data_path)
        items: list[dict] = []
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
                    items = [item for item in loaded["items"] if isinstance(item, dict)]
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                items = []
        start = 0
        if cursor:
            for index, item in enumerate(items):
                if item.get("item_id") == cursor:
                    start = index + 1
                    break
        page = items[start : start + limit]
        next_cursor = page[-1]["item_id"] if len(page) == limit and start + limit < len(items) else None
        return {
            "job_id": job_id,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "items": page,
        }


_SERVICE: OperationService | None = None
_SERVICE_LOCK = threading.Lock()


def get_operation_service(data_path: Path | None = None) -> OperationService:
    global _SERVICE
    if data_path is not None:
        return OperationService(data_path)
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = OperationService()
        return _SERVICE


def reset_operation_service_for_tests() -> None:
    """Drop the process-global singleton so tests can isolate operations.json."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None
