#!/usr/bin/env python3
"""F13/F14: signed definitions channel and background update route tests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers.settings import SettingsHandlers  # noqa: E402


class DummyHandler(SettingsHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload))


class SignedChannelAndUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.state = {"games": [], "settings": {}}

    def test_defs_channel_status_and_update_routes(self):
        self.state["settings"] = {
            "emulator_defs_update_enabled": True,
            "emulator_defs_channel_url": "https://example.invalid/defs.json",
            "emulator_defs_channel_sig_url": "https://example.invalid/defs.json.sig",
        }
        with mock.patch("handlers.settings.load_state_view", return_value=self.state), mock.patch(
            "handlers.settings.defs_channel_status",
            return_value={"defs_dir": str(self.root), "adapters": 2, "errors": 1},
        ):
            handler = DummyHandler()
            handler._api_get_api_v2_emulators_defs_channel(mock.Mock(query=""))
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["adapters"], 2)

        with mock.patch("handlers.settings.load_state_view", return_value=self.state), mock.patch(
            "handlers.settings.fetch_defs_channel", return_value={"version": "2026.09.2", "applied": ["a.yaml"]}
        ) as fetch, mock.patch(
            "handlers.settings.transact_state",
            side_effect=lambda mutate: (self.state, mutate(self.state)),
        ):
            handler = DummyHandler()
            handler._api_post_api_v2_emulators_defs_channel_update({})
        self.assertEqual(handler.responses[-1][1]["version"], "2026.09.2")
        fetch.assert_called_once_with("https://example.invalid/defs.json", "https://example.invalid/defs.json.sig")
        self.assertEqual(self.state["settings"]["emulator_defs_version"], "2026.09.2")
        self.assertTrue(self.state["settings"]["emulator_defs_last_check"])

    def test_defs_channel_update_requires_toggle_and_urls(self):
        with mock.patch("handlers.settings.load_state_view", return_value=self.state):
            with self.assertRaises(ValueError):
                DummyHandler()._api_post_api_v2_emulators_defs_channel_update({})
        self.state["settings"] = {"emulator_defs_update_enabled": True}
        with mock.patch("handlers.settings.load_state_view", return_value=self.state):
            with self.assertRaises(ValueError):
                DummyHandler()._api_post_api_v2_emulators_defs_channel_update({})

    def test_update_download_status_and_job(self):
        self.state["settings"] = {"update_auto_download": True}
        with mock.patch("handlers.settings.load_state_view", return_value=self.state):
            handler = DummyHandler()
            handler._api_get_api_v2_update_download_status(mock.Mock(query=""))
        self.assertTrue(handler.responses[-1][1]["auto_download"])
        self.assertIn("current", handler.responses[-1][1])

        fake_job = {"job_id": "job-42"}
        with mock.patch("handlers.settings.load_state_view", return_value=self.state), mock.patch.dict(
            os.environ, {"APPIMAGE": str(self.root / "OpenBox.AppImage")}
        ), mock.patch("handlers.settings.JOB_MANAGER.submit", return_value=fake_job) as submit:
            handler = DummyHandler()
            handler._api_post_api_v2_update_download({})
        self.assertEqual(handler.responses[-1][1]["job_id"], "job-42")
        self.assertEqual(submit.call_args[0][0], "update-download")

    def test_update_download_requires_toggle_and_appimage(self):
        with mock.patch("handlers.settings.load_state_view", return_value=self.state):
            with self.assertRaises(ValueError):
                DummyHandler()._api_post_api_v2_update_download({})
        self.state["settings"] = {"update_auto_download": True}
        with mock.patch("handlers.settings.load_state_view", return_value=self.state), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                DummyHandler()._api_post_api_v2_update_download({})


if __name__ == "__main__":
    unittest.main()
