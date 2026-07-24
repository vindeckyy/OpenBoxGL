"""Smoke tests for parity API routes."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugin_catalog import load_local_catalog


class ParityApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = self.tempdir.name
        from openbox import save_state
        from web_app import Handler

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        self.handler = object.__new__(Handler)
        self.handler.send_json = mock.Mock()

    def tearDown(self):
        self.tempdir.cleanup()

    def payload(self, response):
        return response[0][1]

    def test_plugin_catalog_route(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.send_json = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/plugins/catalog"
        handler.headers = {}
        handler.do_GET()
        handler.send_json.assert_called_once()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["catalog"])

    def test_storefront_import_route(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.body = mock.Mock(return_value={"source": "steam", "installed_only": True, "uninstalled_only": False})
        handler.send_json = mock.Mock()
        with mock.patch("web_app.storefront_catalog", return_value=[]):
            with mock.patch("web_app.catalog_entries_to_games", return_value=[]):
                Handler.import_storefront_catalog(handler, handler.body())
        handler.send_json.assert_called_with(200, {"added": 0, "found": 0, "imported": 0})

    def test_local_plugin_catalog_file(self):
        catalog = load_local_catalog()
        self.assertTrue(any(item.get("id") == "openbox.library-stats" for item in catalog))


if __name__ == "__main__":
    unittest.main()
