#!/usr/bin/env python3
"""Real-HTTP parity and boundary tests for the v2 library search route."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SearchHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import openbox
        import web_app
        from pkg.state import cache

        cls.openbox = openbox
        cls.web_app = web_app
        cls.save_state = staticmethod(openbox.save_state)
        cls.read_model = cache.SQLITE_READ_MODEL
        cls.read_model.close()
        cls.read_model._db_path = Path(cls.tempdir.name) / "sqlite_readmodel.db"
        cls.read_model._enabled = False
        cls.read_model._signature = None
        web_app.TOKEN = "search-http-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.read_model.close()
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.save_state(self._corpus())
        self.read_model.invalidate()

    @staticmethod
    def _corpus():
        return {
            "games": [
                {
                    "game_id": "hidden-alpha",
                    "name": "Zeta Alpha",
                    "title": "Wrong Title",
                    "hidden": True,
                    "genre": "Action, Adventure",
                },
                {
                    "game_id": "visible-alpha",
                    "name": "alpha: beta",
                    "platform": "Steam",
                    "genre": "Action",
                },
                {
                    "game_id": "unicode",
                    "name": "Straße Quest",
                    "platform": "Console",
                    "genre": "Puzzle",
                },
                {
                    "game_id": "metadata-only",
                    "name": "Canonical Name",
                    "title": "Wrong Title",
                    "platform": "Alpha Platform",
                },
            ],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }

    def request(self, path, token="search-http-token"):
        headers = {}
        if token is not None:
            headers["X-OpenBox-Token"] = token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def _set_enabled(self, enabled):
        self.read_model._enabled = enabled
        self.read_model.invalidate()

    def test_auth_required(self):
        status, payload = self.request("/api/v2/library/search?q=alpha", token=None)
        assert status == 403
        assert payload["error"] == "Unauthorized"

    def test_enabled_and_disabled_search_and_facets_match(self):
        query = "/api/v2/library/search?" + urllib.parse.urlencode({"q": "ALPHA", "limit": 20})
        bodies = {}
        facets = {}
        for enabled in (False, True):
            self._set_enabled(enabled)
            status, payload = self.request(query)
            assert status == 200
            bodies[enabled] = payload
            status, payload = self.request("/api/explorer/facets?field=genre")
            assert status == 200
            facets[enabled] = payload

        assert [game["name"] for game in bodies[False]["results"]] == [
            "Zeta Alpha",
            "alpha: beta",
        ]
        assert bodies[False]["results"] == bodies[True]["results"]
        assert bodies[False]["count"] == bodies[True]["count"] == 2
        assert facets[False] == facets[True]
        assert facets[False]["facets"] == [
            {"value": "Action", "count": 1},
            {"value": "Puzzle", "count": 1},
        ]

    def test_query_bounds_empty_shape_and_malformed_limit_on_both_paths(self):
        many = {
            "games": [{"game_id": f"g-{index}", "name": f"Alpha {index}"} for index in range(205)],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        }
        self.save_state(many)
        for enabled in (False, True):
            self._set_enabled(enabled)
            status, payload = self.request("/api/v2/library/search?q=alpha&limit=0")
            assert status == 200
            assert len(payload["results"]) == payload["count"] == 1

            status, payload = self.request("/api/v2/library/search?q=alpha&limit=999")
            assert status == 200
            assert len(payload["results"]) == payload["count"] == 200

            status, payload = self.request("/api/v2/library/search?q=alpha&limit=bad")
            assert status == 400
            assert payload["code"] == "BAD_REQUEST"

            status, payload = self.request("/api/v2/library/search?q=")
            assert status == 200
            assert payload["results"] == []
            assert payload["count"] == 0


def run_all_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SearchHttpTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_all_tests())
