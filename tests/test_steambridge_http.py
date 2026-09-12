"""Route-level tests for the safe Steam Bridge preview/apply contract."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest, Conflict  # noqa: E402
from handlers import steambridge  # noqa: E402
from pkg.parity.parity_steam_bridge import (  # noqa: E402
    ShortcutsStaleError,
    encode_shortcuts,
    make_shortcut,
)


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class SteamBridgeRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "shortcuts.vdf"
        self.path.write_bytes(encode_shortcuts({
            "shortcuts": {
                "0": make_shortcut("Foreign", "/usr/bin/steam", appid=42),
            }
        }))
        self.state = {"games": [{"game_id": "g1", "name": "OpenBox Game"}]}

    def tearDown(self):
        self.tempdir.cleanup()

    def test_status_reports_bridge_and_foreign_counts(self):
        handler = Handler()
        with mock.patch.object(steambridge, "_path", return_value=self.path):
            steambridge.steambridge_status(handler, SimpleNamespace(query=""))
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["openbox_count"], 0)
        self.assertEqual(payload["foreign_count"], 1)

    def test_preview_and_apply_use_the_same_reviewed_plan(self):
        preview_handler = Handler()
        with mock.patch.object(steambridge, "_path", return_value=self.path), mock.patch.object(
            steambridge, "load_state_view", return_value=self.state
        ):
            steambridge.steambridge_preview(
                preview_handler,
                {"path": str(self.path), "game_ids": ["g1"], "launcher_exe": "/usr/bin/openbox"},
            )
        plan = preview_handler.responses[-1][1]
        self.assertTrue(plan["preview"])
        self.assertEqual(plan["added"], 1)

        apply_handler = Handler()
        with mock.patch.object(steambridge, "_path", return_value=self.path):
            steambridge.steambridge_apply(apply_handler, {"path": str(self.path), "plan": plan})
        result = apply_handler.responses[-1][1]
        self.assertTrue(result["written"])
        self.assertEqual(result["added"], 1)

    def test_remove_preview_and_apply_keep_the_shortcuts_file(self):
        preview_handler = Handler()
        with mock.patch.object(steambridge, "_path", return_value=self.path):
            steambridge.steambridge_remove_preview(
                preview_handler,
                {"path": str(self.path), "targets": [42]},
            )
        plan = preview_handler.responses[-1][1]
        self.assertEqual(plan["removed"], 0)

        # A bridge entry is needed to exercise the mutating removal path.
        from pkg.parity.parity_steam_bridge import appid_for

        self.path.write_bytes(encode_shortcuts({
            "shortcuts": {
                "0": make_shortcut("Bridge", "/usr/bin/openbox", appid=appid_for("/usr/bin/openbox", "Bridge"), openbox_game_id="bridge"),
            }
        }))
        with mock.patch.object(steambridge, "_path", return_value=self.path):
            steambridge.steambridge_remove_preview(
                preview_handler,
                {"path": str(self.path), "targets": ["Bridge"]},
            )
        plan = preview_handler.responses[-1][1]
        self.assertEqual(plan["removed"], 1)
        apply_handler = Handler()
        with mock.patch.object(steambridge, "_path", return_value=self.path):
            steambridge.steambridge_remove(apply_handler, {"path": str(self.path), "plan": plan})
        self.assertTrue(apply_handler.responses[-1][1]["written"])
        self.assertTrue(self.path.is_file())

    def test_invalid_and_stale_requests_have_stable_errors(self):
        with self.assertRaises(BadRequest) as raised:
            steambridge.steambridge_preview(Handler(), None)
        self.assertEqual(raised.exception.code, "STEAMBRIDGE_INVALID_REQUEST")

        handler = Handler()
        with mock.patch.object(steambridge, "_path", return_value=self.path), mock.patch.object(
            steambridge,
            "preview_shortcuts",
            side_effect=ShortcutsStaleError("changed"),
        ):
            with self.assertRaises(Conflict) as raised:
                steambridge.steambridge_preview(handler, {"path": str(self.path)})
        self.assertEqual(raised.exception.code, "STEAMBRIDGE_PREVIEW_STALE")

        with self.assertRaises(BadRequest) as raised:
            steambridge.steambridge_apply(Handler(), {"path": str(self.path)})
        self.assertEqual(raised.exception.code, "STEAMBRIDGE_PLAN_REQUIRED")

        with self.assertRaises(BadRequest) as raised:
            steambridge.steambridge_remove_preview(Handler(), {"path": str(self.path), "targets": []})
        self.assertEqual(raised.exception.code, "STEAMBRIDGE_TARGET_REQUIRED")

    def test_routes_and_runtime_manifest_include_the_bridge(self):
        from routes import GET_TABLE, POST_TABLE
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/steambridge/status"], "handlers.steambridge.steambridge_status")
        self.assertEqual(POST_TABLE["/api/v2/steambridge/preview"], "handlers.steambridge.steambridge_preview")
        self.assertTrue(any(route.path == "/api/v2/steambridge/apply" for route in all_routes()))
        self.assertIn("handlers/steambridge.py", (ROOT / "runtime_modules.txt").read_text())


if __name__ == "__main__":
    unittest.main()
