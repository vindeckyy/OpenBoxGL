#!/usr/bin/env python3
"""Boot-path tests for web_app.main().

Covers the 1.13.0 recovery contract: a corrupt library.json must not stop the
server from starting, so the recovery UI can restore a backup over HTTP.
These tests call main() in-process with a fake HTTP server that raises
KeyboardInterrupt immediately, which runs the real startup and teardown paths.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class FakeServer:
    def __init__(self, address, handler):
        self.server_address = ("127.0.0.1", 0)
        self.handler = handler

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


class WebAppBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="openbox-boot.")
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app

        cls.web_app = web_app

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def _run_main(self, configured_commands=None):
        manager = mock.Mock()
        commands = configured_commands or (lambda *args, **kwargs: None)
        with (
            mock.patch.object(self.web_app, "ThreadingHTTPServer", FakeServer),
            mock.patch.object(self.web_app, "JOB_MANAGER", manager),
            mock.patch.object(self.web_app, "_auto_backup_worker", lambda: None),
            mock.patch.object(self.web_app, "run_configured_commands", commands),
            mock.patch.object(self.web_app, "shutdown_webhooks"),
            mock.patch.object(sys, "argv", ["web_app.py", "--no-browser"]),
        ):
            self.web_app.main()
        return manager

    def test_main_starts_and_tears_down_with_valid_state(self):
        from openbox import save_state

        save_state({
            "games": [{"name": "Boot", "path": "/bin/true"}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        manager = self._run_main()
        self.assertTrue(manager.submit.called)
        self.assertFalse((self.web_app.DATA.parent / "server.token").exists())
        self.assertFalse((self.web_app.DATA.parent / "server.port").exists())

    def test_main_starts_in_recovery_mode_with_corrupt_state(self):
        from openbox import STATE_STORE

        STATE_STORE.path.write_text("{ definitely not json", encoding="utf-8")
        calls = {"count": 0}

        def commands(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                from state_store import StateCorruptError

                raise StateCorruptError("startup commands must be skipped")

        with mock.patch("builtins.print") as _printed:
            manager = self._run_main(commands)
        # Recovery mode must not start the auto-import worker.
        self.assertFalse(manager.submit.called)


if __name__ == "__main__":
    unittest.main()
