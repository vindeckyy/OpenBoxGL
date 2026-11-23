"""Local diagnostic logging with secret redaction."""

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_NAME = "openbox"
LOG_FILE = "openbox.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 4
_SECRET = re.compile(r'(?i)(token|password|secret|api[_-]?key|authorization|client[_-]?secret|ra[\s]+(?:api[\s]+)?key)([=:]\s*)([^\s,&"\']+)')
_JSON_SECRET = re.compile(r'(?i)("(?:token|password|secret|api[_-]?key|authorization|client[_-]?secret)"\s*:\s*")[^"]*')
_PYTHON_SECRET = re.compile(r"(?i)(['\"]?(?:token|password|secret|api[_-]?key|authorization|client[_-]?secret)['\"]?\s*[:=]\s*['\"]?)[^\s,}\]\"']+")
_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+")
logging.getLogger(LOG_NAME).addHandler(logging.NullHandler())


def redact(value):
    text = str(value)
    text = _BEARER.sub(r'\1<redacted>', text)
    text = _JSON_SECRET.sub(r'\1<redacted>', text)
    text = _PYTHON_SECRET.sub(r'\1<redacted>', text)
    return _SECRET.sub(r'\1\2<redacted>', text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        copy = logging.makeLogRecord(record.__dict__.copy())
        copy.msg = redact(record.getMessage())
        copy.args = ()
        return redact(super().format(copy))

    def formatException(self, exc_info):
        return redact(super().formatException(exc_info))


def diagnostic_log_path(data_dir):
    return Path(data_dir) / LOG_FILE


def read_diagnostic_log(data_dir, limit=250_000):
    path = diagnostic_log_path(data_dir)
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - limit))
            return source.read().decode("utf-8", errors="replace")
    except OSError:
        return "No diagnostic log has been written yet."


def configure_logging(data_dir):
    logger = logging.getLogger(LOG_NAME)
    path = diagnostic_log_path(data_dir)
    if any(getattr(handler, "_openbox_diagnostic", False) for handler in logger.handlers):
        return logger
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    handler._openbox_diagnostic = True
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    def uncaught(exc_type, exc_value, exc_traceback):
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

    def uncaught_thread(args):
        logger.critical("Uncaught exception in thread %s", args.thread.name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = uncaught
    threading.excepthook = uncaught_thread
    logger.info("Diagnostic logging started at %s", path)
    return logger
