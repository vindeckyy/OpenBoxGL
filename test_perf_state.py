"""Regression tests for library projection caching and read-only state views.

Phase 2b: public_state is cached until the library file, media epoch, or
plugin epoch changes; read-only routes share one state view; the file probe
cache has a long TTL and is cleared on mutations; every field the browser
client reads stays present in the projected games.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class PerfStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        from openbox import save_state
        from web_app import Handler, MEDIA_EPOCH, PLUGIN_EPOCH, STATE_STORE

        cls.Handler = Handler
        cls.MEDIA_EPOCH = MEDIA_EPOCH
        cls.PLUGIN_EPOCH = PLUGIN_EPOCH
        cls.STATE_STORE = STATE_STORE
        cls.save_state = staticmethod(save_state)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def setUp(self):
        self.MEDIA_EPOCH["value"] = 0
        self.PLUGIN_EPOCH["value"] = 0
        self.save_state({
            "games": [
                {"game_id": "g1", "name": "Alpha", "path": "/bin/true", "cover": ""},
                {"game_id": "g2", "name": "Beta", "path": "/bin/false"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })

    def test_public_state_cached_until_change(self):
        from web_app import public_state

        first = public_state()
        second = public_state()
        self.assertIs(first, second)
        self.assertEqual(len(first["games"]), 2)
        self.assertIn("media_epoch", first)
        self.assertIn("discovery", first)
        self.assertIn("settings", first)

        self.save_state({
            "games": [
                {"game_id": "g1", "name": "Alpha", "path": "/bin/true", "cover": ""},
                {"game_id": "g2", "name": "Beta", "path": "/bin/false"},
                {"game_id": "g3", "name": "Gamma", "path": "/bin/ls"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        third = public_state()
        self.assertIsNot(first, third)
        self.assertEqual(len(third["games"]), 3)

    def test_public_state_bytes_match_payload(self):
        from web_app import public_state, public_state_bytes

        raw = public_state_bytes()
        self.assertEqual(json.loads(raw), public_state())
        self.assertIs(public_state_bytes(), raw)

    def test_media_epoch_invalidates_public_state(self):
        from web_app import bump_media_epoch, public_state

        first = public_state()
        bump_media_epoch()
        second = public_state()
        self.assertIsNot(first, second)
        self.assertEqual(second["media_epoch"], 1)

    def test_plugin_epoch_invalidates_public_state(self):
        from web_app import public_state

        first = public_state()
        self.PLUGIN_EPOCH["value"] += 1
        self.assertIsNot(public_state(), first)

    def test_load_state_view_shared_until_change(self):
        from web_app import load_state_view

        first = load_state_view()
        second = load_state_view()
        self.assertIs(first, second)
        self.assertEqual(len(first["games"]), 2)

        self.save_state({
            "games": [{"game_id": "g1", "name": "Alpha", "path": "/bin/true"}],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        third = load_state_view()
        self.assertIsNot(first, third)
        self.assertEqual(len(third["games"]), 1)

    def test_probe_ttl_and_clear(self):
        from web_app import FILE_PROBE_CACHE, FILE_PROBE_TTL, bump_media_epoch, clear_file_probe_cache

        self.assertEqual(FILE_PROBE_TTL, 60.0)
        FILE_PROBE_CACHE[("/tmp/x", False)] = (0.0, True)
        clear_file_probe_cache()
        self.assertEqual(FILE_PROBE_CACHE, {})
        FILE_PROBE_CACHE[("/tmp/y", False)] = (0.0, True)
        bump_media_epoch()
        self.assertEqual(FILE_PROBE_CACHE, {})

    def test_public_state_projection_matches_client_fields(self):
        from web_app import public_state

        game = public_state()["games"][0]
        client = Path(__file__).parent / "index.html"
        referenced = set()
        for match in re.findall(r"game\.([A-Za-z_][A-Za-z0-9_]*)", client.read_text()):
            if match not in {"id"}:
                referenced.add(match)
        missing = sorted(referenced - set(game.keys()))
        self.assertEqual(missing, [], f"fields the UI reads are missing from the projection: {missing}")

    def test_related_route_uses_view_cache(self):
        from web_app import Handler, load_state_view

        with mock.patch("web_app.load_state_view", wraps=load_state_view) as view:
            handler = object.__new__(Handler)
            handler.send_json = mock.Mock()
            handler.authorized = mock.Mock(return_value=True)
            handler.do_GET = Handler.do_GET.__get__(handler, Handler)
            handler.path = "/api/related?id=0&token=test"
            handler.do_GET()
            view.assert_called_once()
            status, payload = handler.send_json.call_args[0]
            self.assertEqual(status, 200)
            self.assertIn("ids", payload)


if __name__ == "__main__":
    unittest.main()
