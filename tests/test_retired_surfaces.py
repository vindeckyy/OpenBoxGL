"""Retired API surfaces (ADR 0103): v1, unversioned paths, and legacy host names.

The wire behavior matters more than the constants: an unauthenticated caller must
still get 403, an authenticated caller must get a 410 problem document with a
usable replacement, and the v2/host twins must keep working.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RetiredSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app

        cls.web_app = web_app
        web_app.TOKEN = "retired-test-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def request(self, path, method="GET", body=None, token="retired-test-token"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-OpenBox-Token"] = token
        req = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            payload = error.read().decode()
            error.close()
            return error.code, json.loads(payload) if payload else {}

    def test_v1_is_gone_and_points_at_v2(self):
        status, payload = self.request("/api/v1/library")
        self.assertEqual(status, 410)
        self.assertEqual(payload["code"], "V1_API_REMOVED")
        self.assertEqual(payload["context"]["use"], "/api/v2/library")
        self.assertEqual(payload["type"], "https://openbox.local/problems/v1-api-removed")

    def test_unversioned_public_path_is_gone(self):
        status, payload = self.request("/api/library")
        self.assertEqual(status, 410)
        self.assertEqual(payload["code"], "API_VERSION_REQUIRED")
        self.assertEqual(payload["context"]["use"], "/api/v2/library")

    def test_legacy_host_path_is_gone(self):
        status, payload = self.request("/api/launch", method="POST", body={"id": 0})
        self.assertEqual(status, 410)
        self.assertEqual(payload["code"], "HOST_CHANNEL_RETIRED")
        self.assertEqual(payload["context"]["use"], "/api/host/launch")

    def test_unknown_path_is_404_with_auth(self):
        status, payload = self.request("/api/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "ROUTE_NOT_FOUND")

    def test_retired_paths_still_require_auth(self):
        for path in ("/api/v1/library", "/api/library", "/api/launch"):
            with self.subTest(path=path):
                status, _ = self.request(path, token="")
                self.assertEqual(status, 403)

    def test_v2_and_host_twins_keep_working(self):
        status, payload = self.request("/api/v2/version")
        self.assertEqual(status, 200)
        self.assertEqual(payload["api_version"], 2)
        host_status, host_payload = self.request("/api/host/capabilities")
        self.assertEqual(host_status, 200)
        self.assertIn("features", host_payload)

    def test_backup_format_one_write_is_refused_before_a_job_is_queued(self):
        status, payload = self.request("/api/v2/backup/create", method="POST", body={"format": 1, "items": ["library"]})
        self.assertEqual(status, 410)
        self.assertEqual(payload["code"], "BACKUP_FORMAT_RETIRED")
        self.assertEqual(payload["context"]["writable"], [2])

    def test_the_old_environment_variable_does_not_restore_v1(self):
        previous = os.environ.get("OPENBOX_ENABLE_V1_API")
        os.environ["OPENBOX_ENABLE_V1_API"] = "1"
        try:
            status, payload = self.request("/api/v1/library")
        finally:
            if previous is None:
                os.environ.pop("OPENBOX_ENABLE_V1_API", None)
            else:
                os.environ["OPENBOX_ENABLE_V1_API"] = previous
        self.assertEqual(status, 410)
        self.assertEqual(payload["code"], "V1_API_REMOVED")


if __name__ == "__main__":
    unittest.main()
