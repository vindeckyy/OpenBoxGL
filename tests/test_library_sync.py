#!/usr/bin/env python3
"""Regression tests for the disabled full-library synchronization routes."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from cloud_sync import (  # noqa: E402
    CloudSyncError,
    LIBRARY_SYNC_FILE,
    LibrarySyncUnavailable,
    publish_library,
    pull_library,
)


class DirectLibrarySyncGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.target = self.directory / LIBRARY_SYNC_FILE
        self.remote_bytes = b'{"format": 2, "games": {"id:remote": {}}}'
        self.target.write_bytes(self.remote_bytes)

    def tearDown(self):
        self._tmp.cleanup()

    def assert_unavailable(self, operation, state):
        original_state = copy.deepcopy(state)
        with self.assertRaises(LibrarySyncUnavailable) as context:
            operation(state, self.directory, device_id="test-device")
        self.assertIsInstance(context.exception, CloudSyncError)
        self.assertEqual(context.exception.code, "LIBRARY_SYNC_UNAVAILABLE")
        self.assertIn("unavailable", context.exception.message.lower())
        self.assertEqual(state, original_state)
        self.assertEqual(self.target.read_bytes(), self.remote_bytes)

    def test_publish_rejects_before_local_or_remote_io(self):
        self.assert_unavailable(
            publish_library,
            {"games": [{"game_id": "local", "name": "Keep"}], "settings": {"marker": "keep"}},
        )

    def test_pull_rejects_before_local_or_remote_io(self):
        self.assert_unavailable(
            pull_library,
            {"games": [{"game_id": "local", "name": "Keep"}], "settings": {"marker": "keep"}},
        )

    def test_rejection_does_not_depend_on_folder_or_body_shape(self):
        for operation in (publish_library, pull_library):
            for state, folder in ((None, self.directory), ([], self.directory / "missing")):
                with self.assertRaises(LibrarySyncUnavailable) as context:
                    operation(state, folder)
                self.assertEqual(context.exception.code, "LIBRARY_SYNC_UNAVAILABLE")
        self.assertEqual(self.target.read_bytes(), self.remote_bytes)


class RouteRegistrationAndHandlerTests(unittest.TestCase):
    def test_routes_remain_registered(self):
        from routes import POST_TABLE

        self.assertIn("/api/v2/library/sync/publish", POST_TABLE)
        self.assertIn("/api/v2/library/sync/pull", POST_TABLE)

    def test_handlers_return_structured_503_without_loading_state(self):
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def __init__(self):
                self.sent = []

            def send_json(self, code, body):
                self.sent.append((code, body))

        for method_name in (
            "_api_post_api_v2_library_sync_publish",
            "_api_post_api_v2_library_sync_pull",
        ):
            handler = MockHandler()
            with patch("openbox.load_state", side_effect=AssertionError("state must not load")):
                getattr(handler, method_name)(None)
            self.assertEqual(handler.sent, [(
                503,
                {
                    "error": "Full library synchronization is unavailable pending a safe replacement.",
                    "code": "LIBRARY_SYNC_UNAVAILABLE",
                },
            )])

    def test_handlers_return_503_even_without_cloud_folder(self):
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def send_json(self, code, body):
                self.sent = code, body

        for method_name in (
            "_api_post_api_v2_library_sync_publish",
            "_api_post_api_v2_library_sync_pull",
        ):
            handler = MockHandler()
            getattr(handler, method_name)({})
            self.assertEqual(handler.sent[0], 503)
            self.assertEqual(handler.sent[1]["code"], "LIBRARY_SYNC_UNAVAILABLE")


class RealHttpLibrarySyncGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app

        from openbox import save_state

        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        cls.web_app.TOKEN = "library-sync-test-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.web_app.Handler)
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
            "games": [{"game_id": "local", "name": "Keep"}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        self.state_path = Path(self.tempdir.name) / "library.json"
        self.remote_path = Path(self.tempdir.name) / LIBRARY_SYNC_FILE
        self.remote_bytes = b'{"format": 2, "games": {"id:remote": {}}}'
        self.remote_path.write_bytes(self.remote_bytes)
        self.state_bytes = self.state_path.read_bytes()

    def request(self, path, body=b"", token="library-sync-test-token", content_type="application/json"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={"X-OpenBox-Token": token, "Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def assert_no_mutation(self):
        self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
        self.assertEqual(self.remote_path.read_bytes(), self.remote_bytes)

    def test_authenticated_publish_and_pull_return_503_without_mutation(self):
        body = json.dumps({"device_id": "test-device"}).encode()
        for endpoint in ("publish", "pull"):
            status, payload = self.request(f"/api/v2/library/sync/{endpoint}", body)
            self.assertEqual(status, 503)
            self.assertEqual(payload["code"], "LIBRARY_SYNC_UNAVAILABLE")
            self.assertIn("unavailable", payload["error"].lower())
            self.assert_no_mutation()

    def test_authentication_is_checked_before_guard(self):
        body = b"{}"
        status, payload = self.request("/api/v2/library/sync/publish", body, token="wrong-token")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "Unauthorized")
        self.assert_no_mutation()

    def test_malformed_body_is_rejected_without_reaching_guard(self):
        status, payload = self.request("/api/v2/library/sync/pull", b"not-json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "BAD_REQUEST")
        self.assert_no_mutation()

    def test_non_object_body_is_rejected_without_mutation(self):
        status, payload = self.request("/api/v2/library/sync/publish", b"[]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "BAD_REQUEST")
        self.assert_no_mutation()


if __name__ == "__main__":
    unittest.main()
