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
        self.assertIn("handlers/steambridge.py", (ROOT / "runtime_modules.txt").read_text(encoding="utf-8"))


class SteamBridgeGridArtTests(unittest.TestCase):
    """F5d: after apply, cached artwork lands in the Steam grid dir."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.config = base / "Steam" / "userdata" / "424242" / "config"
        self.config.mkdir(parents=True)
        self.shortcuts = self.config / "shortcuts.vdf"
        self.shortcuts.write_bytes(encode_shortcuts({"shortcuts": {}}))
        media = base / "media"
        media.mkdir()
        self.cover = media / "cover.png"
        self.cover.write_bytes(b"cover-bytes")
        self.hero = media / "background.jpg"
        self.hero.write_bytes(b"hero-bytes")
        self.logo = media / "clear_logo.png"
        self.logo.write_bytes(b"logo-bytes")
        self.game = {
            "game_id": "g1",
            "name": "Grid Game",
            "cover": str(self.cover),
            "background": str(self.hero),
            "clear_logo": str(self.logo),
        }
        self.state = {"games": [self.game]}

    def tearDown(self):
        self.tempdir.cleanup()

    def _apply(self, path, shortcut_side_effect=None):
        from pkg.parity.parity_steam_bridge import shortcut_from_game  # noqa: E402

        preview_handler = Handler()
        body = {"path": str(path), "game_ids": ["g1"], "launcher_exe": "/usr/bin/openbox"}
        roots = (self.config.parents[2],)  # <tmp>/Steam: the fake Steam root
        with mock.patch.object(steambridge, "_steam_roots", return_value=roots), mock.patch.object(
            steambridge, "load_state_view", return_value=self.state
        ):
            steambridge.steambridge_preview(preview_handler, body)
        plan = preview_handler.responses[-1][1]
        self.assertEqual(plan["added"], 1)
        apply_handler = Handler()
        with mock.patch.object(steambridge, "_steam_roots", return_value=roots), mock.patch.object(
            steambridge, "load_state_view", return_value=self.state
        ):
            if shortcut_side_effect is not None:
                with mock.patch.object(steambridge, "shortcut_from_game", side_effect=shortcut_side_effect):
                    steambridge.steambridge_apply(apply_handler, {**body, "plan": plan})
            else:
                steambridge.steambridge_apply(apply_handler, {**body, "plan": plan})
        result = apply_handler.responses[-1][1]
        self.assertTrue(result["written"])
        appid = shortcut_from_game(self.game, launcher_exe="/usr/bin/openbox")["appid"]
        return result, appid

    def test_apply_writes_capsule_hero_logo(self):
        result, appid = self._apply(self.shortcuts)
        grid = self.config / "grid"
        self.assertTrue((grid / f"{appid}p.png").is_file())
        self.assertTrue((grid / f"{appid}_hero.png").is_file())
        self.assertTrue((grid / f"{appid}_logo.png").is_file())
        # Original bytes copied as-is (v1: no resize), jpg hero keeps its bytes.
        self.assertEqual((grid / f"{appid}p.png").read_bytes(), b"cover-bytes")
        self.assertEqual((grid / f"{appid}_hero.png").read_bytes(), b"hero-bytes")
        self.assertEqual((grid / f"{appid}_logo.png").read_bytes(), b"logo-bytes")
        self.assertEqual(result["grid_art"], {"applied": 1, "skipped": 0})

    def test_missing_artwork_is_skipped_honestly(self):
        self.game.update({"cover": "", "background": "", "clear_logo": str(Path(self.tempdir.name) / "nope.png")})
        result, _ = self._apply(self.shortcuts)
        self.assertEqual(result["grid_art"], {"applied": 0, "skipped": 1})
        grid = self.config / "grid"
        self.assertEqual(list(grid.iterdir()) if grid.is_dir() else [], [])

    def test_grid_dir_outside_detected_accounts_is_never_written(self):
        outside = Path(self.tempdir.name) / "elsewhere" / "config" / "shortcuts.vdf"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(encode_shortcuts({"shortcuts": {}}))
        result, _ = self._apply(outside)
        self.assertEqual(result["grid_art"], {"applied": 0, "skipped": 1})
        self.assertFalse((outside.parent / "grid").exists())

    def test_uncreatable_grid_dir_skips_honestly(self):
        (self.config / "grid").write_text("not a dir")
        result, _ = self._apply(self.shortcuts)
        self.assertEqual(result["grid_art"], {"applied": 0, "skipped": 1})

    def test_appid_mapping_failure_skips_game(self):
        from pkg.parity.parity_steam_bridge import SteamBridgeError  # noqa: E402

        result, _ = self._apply(self.shortcuts, shortcut_side_effect=SteamBridgeError("no appid"))
        self.assertEqual(result["grid_art"], {"applied": 0, "skipped": 1})


if __name__ == "__main__":
    unittest.main()
