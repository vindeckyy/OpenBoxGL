"""Tests for local diagnostic logging."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openbox_logging import configure_logging, diagnostic_log_path, read_diagnostic_log, redact  # noqa: E402


def test_redaction():
    assert "secret-token" not in redact("token=secret-token password: hunter2")
    assert "<redacted>" in redact('{"api_key":"secret-token"}')
    assert "hunter2" not in redact("{'password': 'hunter2'}")
    assert "secret-token" not in redact("Authorization: Bearer secret-token")
    assert "retroach-key-abc" not in redact("RA key= retroach-key-abc")
    assert "retroach-key-abc" not in redact("RA API KEY= retroach-key-abc")
    assert "igdb-secret" not in redact("client_secret= igdb-secret")
    assert "igdb-secret" not in redact('{"client_secret": "igdb-secret"}')


def test_file_logging():
    with TemporaryDirectory() as directory:
        logger = configure_logging(directory)
        logger.debug("Diagnostic test message")
        assert diagnostic_log_path(directory).is_file()
        assert "Diagnostic test message" in read_diagnostic_log(directory)
        # Windows keeps the log handle open; close it so the temp directory
        # can be removed at the end of the test.
        for handler in list(logger.handlers):
            if getattr(handler, "_openbox_diagnostic", False):
                handler.close()
                logger.removeHandler(handler)


if __name__ == "__main__":
    test_redaction()
    test_file_logging()
    print("logging self-test: ok")
