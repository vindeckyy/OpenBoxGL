#!/usr/bin/env python3
"""Reliability #11: offline metadata sync must surface error + retry in durable operations.

Network-stubbed: sync_database raises URLError (no real network). The legacy
METADATA_JOB facade must report error, AND the durable operation
(metadata.db_sync in operations.json) must finish terminal-error (not done)
with can_retry, so the job panel offers retry via POST /api/v2/jobs/retry.
"""
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401  # register flat-import finder

import handlers.metadata as hm  # noqa: E402
from pkg.state.operations import (  # noqa: E402
    TERMINAL_STATES,
    OperationService,
    get_operation_service,
    reset_operation_service_for_tests,
)


def main():
    with tempfile.TemporaryDirectory() as directory:
        data_file = Path(directory) / "library.json"
        data_file.write_text('{"games": []}', encoding="utf-8")
        service = OperationService(data_path=data_file)

        import job_manager

        manager = job_manager.JobManager(max_workers=2)
        try:
            handler = _DummyHandler()
            hm.METADATA_JOB.clear()
            with mock.patch.object(job_manager, "_get_operation_service", return_value=service), \
                 mock.patch.object(hm, "JOB_MANAGER", manager), \
                 mock.patch.object(hm, "sync_database", side_effect=URLError("offline")):
                handler.sync_metadata()
                assert handler.responses and handler.responses[-1][0] == 202, handler.responses
                operation = _wait_for_terminal(service)
            assert operation is not None, "no metadata.db_sync operation recorded"
            assert operation["type"] == "metadata.db_sync", operation
            assert operation["state"] == "error", f"offline sync must finish error, got {operation['state']}"
            message = str((operation.get("error") or {}).get("message") or operation.get("message") or "")
            assert "offline" in message.casefold(), f"offline state must name the cause: {message!r}"
            assert operation["can_retry"] is True, "offline job must offer retry"
            # Legacy facade (job panel) mirrors the offline error.
            assert hm.METADATA_JOB.get("state") == "error", dict(hm.METADATA_JOB)
            assert "offline" in str(hm.METADATA_JOB.get("error") or "").casefold()
            # Retry mints a follow-up job rooted at the failed one.
            retry = service.retry(operation["job_id"])
            assert retry["state"] == "queued", retry
            assert retry["retry_of"] == operation["job_id"], retry
            assert retry["root_job_id"] == operation["root_job_id"], retry
            assert retry["type"] == "metadata.db_sync", retry
            assert operation["state"] in TERMINAL_STATES
        finally:
            manager.shutdown(wait=True)
            reset_operation_service_for_tests()
            get_operation_service()
    print("offline sync self-test: ok")


class _DummyHandler(hm.MetadataHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload, kwargs))


def _wait_for_terminal(service, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = service.list_jobs(type_filter="metadata.db_sync", limit=5)
        for job in page["jobs"]:
            if job["state"] in TERMINAL_STATES:
                return job
        time.sleep(0.05)
    page = service.list_jobs(type_filter="metadata.db_sync", limit=5)
    return page["jobs"][0] if page["jobs"] else None


if __name__ == "__main__":
    main()
