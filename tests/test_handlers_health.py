"""Tests for handlers/health.py."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402
from openbox import DATA  # noqa: E402


def make_handler():
    h = web_app.Handler.__new__(web_app.Handler)
    h.responses = []
    h.headers = {"Content-Length": "0"}
    h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h.send_json = lambda status, payload: h.responses.append((status, payload))
    return h


class BackupDiffRouteTest(unittest.TestCase):
    def diff(self, archive):
        h = make_handler()
        h._api_get_api_v2_backup_diff(urlparse(f"/api/v2/backup/diff?archive={quote(archive)}"))
        return h.responses[0]

    def test_missing_archive_is_404(self):
        # Regression: approved_backup_file raises FileNotFoundError rather than
        # returning a falsy value, so the 404 branch was dead and a missing
        # archive escaped as a generic 400 carrying the absolute path.
        missing = DATA.parent / "backups" / "OpenBoxBackup-does-not-exist.zip"
        status, payload = self.diff(str(missing))
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "backup archive not found"})

    def test_unapproved_archive_is_400(self):
        status, payload = self.diff("/definitely/outside/openbox/backup.zip")
        self.assertEqual(status, 400)
        self.assertNotIn("/definitely/outside", payload["error"])

    def test_missing_parameter_is_400(self):
        h = make_handler()
        h._api_get_api_v2_backup_diff(urlparse("/api/v2/backup/diff"))
        self.assertEqual(h.responses[0][0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
