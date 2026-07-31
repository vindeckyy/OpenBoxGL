"""Small standard-library job manager for bounded backend work."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable


LOGGER = logging.getLogger("openbox.jobs")


class JobManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}

    def snapshot(self, name):
        with self._lock:
            return dict(self._jobs.get(name, {}))

    def submit(self, name, worker: Callable[[], dict], *, replace=False):
        with self._lock:
            current = self._jobs.get(name, {})
            if current.get("state") in {"queued", "running"} and not replace:
                return dict(current)
            now = datetime.now(timezone.utc).isoformat()
            job = {
                "name": name,
                "state": "queued",
                "started_at": "",
                "finished_at": "",
                "created_at": now,
                "error": "",
                "attempt": int(current.get("attempt", 0)) + 1,
            }
            self._jobs[name] = job

        def run():
            with self._lock:
                self._jobs[name].update({
                    "state": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                })
            started = time.monotonic()
            try:
                result = worker() or {}
                if not isinstance(result, dict):
                    result = {"result": result}
                with self._lock:
                    self._jobs[name].update(result)
                    self._jobs[name].update({
                        "state": "done",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "duration_seconds": round(time.monotonic() - started, 3),
                    })
            except Exception as error:
                LOGGER.exception("Backend job %s failed", name)
                with self._lock:
                    self._jobs[name].update({
                        "state": "error",
                        "error": str(error),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "duration_seconds": round(time.monotonic() - started, 3),
                    })

        threading.Thread(target=run, name=f"openbox-job-{name}", daemon=True).start()
        return dict(job)
