"""Tests for local diagnostic logging."""

from tempfile import TemporaryDirectory

from openbox_logging import configure_logging, diagnostic_log_path, read_diagnostic_log, redact


def test_redaction():
    assert "secret-token" not in redact("token=secret-token password: hunter2")
    assert "<redacted>" in redact('{"api_key":"secret-token"}')
    assert "hunter2" not in redact("{'password': 'hunter2'}")
    assert "secret-token" not in redact("Authorization: Bearer secret-token")


def test_file_logging():
    with TemporaryDirectory() as directory:
        logger = configure_logging(directory)
        logger.debug("Diagnostic test message")
        assert diagnostic_log_path(directory).is_file()
        assert "Diagnostic test message" in read_diagnostic_log(directory)


if __name__ == "__main__":
    test_redaction()
    test_file_logging()
    print("logging self-test: ok")
