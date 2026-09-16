#!/usr/bin/env python3
"""P1-22: invalid launch template tokens are rejected with a BadRequest."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_errors import BadRequest  # noqa: E402
from handlers.launch import LaunchHandlers, _iter_launch_templates  # noqa: E402


class LaunchTemplateExtractionTests(unittest.TestCase):
    def test_non_dict_payload_yields_nothing(self):
        self.assertEqual(list(_iter_launch_templates(["not", "a", "dict"])), [])

    def test_extracts_top_level_candidate_and_batch_templates(self):
        payload = {
            "launch": "top {path}",
            "candidate": {"launch_command": "candidate {path}"},
            "items": [{"candidate": {"launch": "batch {path}"}}],
        }
        self.assertEqual(
            list(_iter_launch_templates(payload)),
            ["top {path}", "candidate {path}", "batch {path}"],
        )

    def test_ignores_non_string_and_empty_templates(self):
        payload = {"launch": 123, "launch_command": "", "candidate": {"launch": None}}
        self.assertEqual(list(_iter_launch_templates(payload)), [])


class LaunchTokenValidationTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(LaunchHandlers)
        handler.send_json = mock.Mock()
        return handler

    def test_candidate_launch_with_unknown_token_is_rejected(self):
        handler = self._handler()
        payload = {
            "candidate": {
                "candidate_id": "c1",
                "preview_id": "p1",
                "path": "/tmp/rom.nes",
                "platform": "NES",
                "emulator_id": None,
                "adapter_id": None,
                "archive_member": None,
                "launch": "retroarch -L core.so {bogus_token}",
            },
        }
        with self.assertRaises(BadRequest) as caught:
            handler.launch_preflight(payload)
        self.assertIn("bogus_token", str(caught.exception))
        handler.send_json.assert_not_called()

    def test_top_level_launch_command_with_unknown_token_is_rejected(self):
        handler = self._handler()
        with self.assertRaises(BadRequest):
            handler.launch_preflight({"game_id": "g1", "launch_command": "emu {nope}"})

    def test_batch_item_launch_with_unknown_token_is_rejected(self):
        handler = self._handler()
        with self.assertRaises(BadRequest):
            handler.launch_preflight_batch({"items": [{"candidate": {"launch": "emu {nope}"}}]})
        handler.send_json.assert_not_called()

    def test_valid_tokens_reach_preflight(self):
        handler = self._handler()
        expected = {
            "status": "ready",
            "game_id": "g1",
            "candidate_id": None,
            "resolved": {},
            "checks": [],
        }
        with mock.patch("handlers.launch.load_state", return_value={"games": [], "profiles": {}}), \
             mock.patch("handlers.launch.preflight_single", return_value=expected) as preflight:
            handler.launch_preflight({"game_id": "g1", "launch": "retroarch -L core.so {path}"})
        preflight.assert_called_once()
        handler.send_json.assert_called_once_with(200, expected)

    def test_candidate_without_launch_is_not_rejected(self):
        handler = self._handler()
        expected = {
            "status": "ready",
            "game_id": None,
            "candidate_id": "c1",
            "resolved": {},
            "checks": [],
        }
        payload = {
            "candidate": {
                "candidate_id": "c1",
                "preview_id": "p1",
                "path": "/tmp/rom.nes",
                "platform": "NES",
                "emulator_id": None,
                "adapter_id": None,
                "archive_member": None,
            },
        }
        with mock.patch("handlers.launch.load_state", return_value={"games": [], "profiles": {}}), \
             mock.patch("handlers.launch.preflight_single", return_value=expected) as preflight:
            handler.launch_preflight(payload)
        preflight.assert_called_once()
        handler.send_json.assert_called_once_with(200, expected)


if __name__ == "__main__":
    unittest.main()
