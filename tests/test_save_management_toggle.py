#!/usr/bin/env python3
"""Save-management toggle: read routes report disabled, write routes reject."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_errors import BadRequest  # noqa: E402
from handlers.data import DataHandlers  # noqa: E402


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(DataHandlers)
        self.payloads = []
        self.handler.send_json = lambda status, payload: self.payloads.append((status, payload))

    def test_disabled_read_reports_enabled_false(self):
        with mock.patch("handlers.data.load_state_view",
                        return_value={"settings": {"save_management_enabled": False}}):
            self.assertTrue(self.handler._save_management_off(read=True))
        self.assertEqual(self.payloads, [(200, {"enabled": False})])

    def test_disabled_write_rejects(self):
        with mock.patch("handlers.data.load_state_view",
                        return_value={"settings": {"save_management_enabled": False}}):
            with self.assertRaises(BadRequest) as ctx:
                self.handler._save_management_off()
        self.assertIn("Save management is disabled", str(ctx.exception))

    def test_enabled_allows_both_paths(self):
        with mock.patch("handlers.data.load_state_view",
                        return_value={"settings": {"save_management_enabled": True}}):
            self.assertFalse(self.handler._save_management_off(read=True))
            self.assertFalse(self.handler._save_management_off())
        self.assertEqual(self.payloads, [])


if __name__ == "__main__":
    unittest.main()
