"""HTTP contract tests for the engineering-gate routes.

Covers the surfaces added by the polish program: static assets, gzip
library encoding, /api/jobs, /api/diagnostic, and /api/state/recover.
"""

import gzip
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer


class PolishRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app
        from openbox import save_state
        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        web_app.TOKEN = "polish-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.save_state({
            "games": [{"name": "Fixture", "path": "/bin/true"}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })

    def request(self, path, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"X-OpenBox-Token": "polish-token", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_static_assets_serve_with_etag(self):
        status, headers, body = self.request("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get("Content-Type", ""))
        # app.js is an ES module; token is now imported from state.js and scrubbed via history.replaceState
        self.assertIn(b"token", body)
        self.assertIn(b"history.replaceState", body)
    def test_static_assets_reject_unknown_files(self):
        status, _, payload = self.request("/static/evil.bin")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["code"], "ROUTE_NOT_FOUND")

    def test_library_gzip_round_trip(self):
        status, headers, body = self.request("/api/library", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        payload = json.loads(gzip.decompress(body))
        self.assertEqual(payload["games"][0]["name"], "Fixture")

    def test_library_pagination_ignores_full_library_etag(self):
        status, headers, _ = self.request("/api/library")
        self.assertEqual(status, 200)
        status, _, body = self.request(
            "/api/library?offset=0&limit=0",
            {"If-None-Match": headers["ETag"]},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["games"], [])
        self.assertEqual(payload["total_count"], 1)

    def test_library_delta_accepts_stable_game_ids(self):
        status, _, body = self.request("/api/library")
        self.assertEqual(status, 200)
        game_id = json.loads(body)["games"][0]["game_id"]
        status, _, body = self.request(f"/api/library/delta?ids={game_id}")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([game["game_id"] for game in payload["games"]], [game_id])

    def test_jobs_route_lists_jobs(self):
        status, _, payload = self.request("/api/jobs")
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertIn("jobs", data)
        self.assertIn("history", data)

    def test_diagnostic_route_returns_report(self):
        status, _, payload = self.request("/api/diagnostic")
        self.assertEqual(status, 200)
        data = json.loads(payload)
        # The endpoint returns the report text; parse its JSON envelope.
        report = json.loads(data["report"])
        self.assertEqual(report["report"], "openbox-diagnostic")
        self.assertIn("system", report)

    def test_state_recover_dry_run(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/state/recover",
            data=json.dumps({"dry_run": True}).encode(),
            headers={"X-OpenBox-Token": "polish-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["dry_run"])
        self.assertIn("snapshots", payload)
        self.assertFalse(payload["needs_recovery"])

    def post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"X-OpenBox-Token": "polish-token", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_corrupt_state_serves_503_then_recovers_from_backup(self):
        # A second save creates the last-known-good .bak from the first save.
        self.save_state({
            "games": [{"name": "Latest", "path": "/bin/true"}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        from openbox import STATE_STORE

        state_path = STATE_STORE.path
        state_path.write_text("{ not valid json", encoding="utf-8")

        status, _, body = self.request("/api/library")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["code"], "STATE_UNAVAILABLE")

        status, payload = self.post("/api/state/recover", {"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["needs_recovery"])
        self.assertTrue(payload["backup_available"])

        status, payload = self.post("/api/state/recover", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        status, _, body = self.request("/api/library")
        self.assertEqual(status, 200)
        # .bak mirrors the last committed write, so recovery returns it.
        self.assertEqual(json.loads(body)["games"][0]["name"], "Latest")


if __name__ == "__main__":
    unittest.main()
