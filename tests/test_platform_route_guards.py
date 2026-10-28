#!/usr/bin/env python3
"""Linux-only route families answer off Linux instead of failing at click time.

The capability contract (ADR 0086) hides these controls on Windows and macOS,
and the routes behind them must reject a stale client the same way. Tests patch
the owning handler module's bound flag, not ``pkg.platform_compat``, because
handlers bind ``IS_*`` and feature state at import time.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest  # noqa: E402
from routes.registry import all_routes  # noqa: E402

import handlers.faugus as faugus_handler  # noqa: E402
import handlers.launch as launch_handler  # noqa: E402
import handlers.wine as wine_handler  # noqa: E402
import pkg.parity.parity_faugus as parity_faugus  # noqa: E402
import pkg.parity.parity_flatpak_grant as flatpak_grant  # noqa: E402


LINUX_ONLY_PREFIXES = ("/api/wine/", "/api/faugus/", "/api/v2/launch/grant")


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class WineHandler(wine_handler.WineHandlers, Handler):
    def __init__(self):
        self.responses = []


class FaugusHandler(faugus_handler.FaugusHandlers, Handler):
    def __init__(self):
        self.responses = []


class RouteFamilyTests(unittest.TestCase):
    def test_every_linux_only_route_is_in_a_probed_family(self):
        paths = {route.path for route in all_routes()}
        for prefix in LINUX_ONLY_PREFIXES:
            self.assertTrue({path for path in paths if path.startswith(prefix)}, prefix)
        for path in paths:
            lowered = path.casefold()
            if "wine" in lowered or "faugus" in lowered or "/grant" in lowered:
                self.assertTrue(path.startswith(LINUX_ONLY_PREFIXES), path)

    def test_wine_routes_report_unavailable_off_linux(self):
        handler = WineHandler()
        with mock.patch.object(wine_handler, "HAS_WINE", False):
            handler._api_get_api_wine_prefixes({})
            handler._api_get_api_wine_protons({})
            with mock.patch.object(wine_handler, "load_state_view", return_value={}), \
                 mock.patch.object(wine_handler, "game_from_query", return_value={"name": "G"}):
                handler._api_get_api_wine_prefix_for_game(mock.Mock(query="game_id=g"))
        self.assertEqual(handler.responses, [
            (200, {"prefixes": [], "available": False}),
            (200, {"protons": [], "available": False}),
            (200, {"prefix": "", "available": False}),
        ])

    def test_faugus_routes_stay_structured_off_linux(self):
        handler = FaugusHandler()
        with mock.patch.object(parity_faugus, "IS_LINUX", False), \
             mock.patch.object(faugus_handler, "merge_imported_games", return_value=(0, 0)) as merge, \
             mock.patch.object(faugus_handler, "load_state", return_value={"games": []}), \
             mock.patch.object(faugus_handler, "clear_file_probe_cache"):
            handler._api_get_api_faugus_status({})
            handler._api_get_api_faugus_scan({})
            handler._api_post_api_faugus_import({})
        self.assertEqual(handler.responses[0], (200, {"installed": False, "data_dirs": []}))
        self.assertEqual(handler.responses[1], (200, {"games": [], "count": 0}))
        self.assertEqual(handler.responses[2][1]["added"], 0)
        merge.assert_called_once_with([], mock.ANY)

    def test_the_flatpak_grant_is_refused_off_linux(self):
        handler = launch_handler.LaunchHandlers()
        handler.send_json = lambda status, payload: self.fail("the route must not answer")
        with mock.patch.object(flatpak_grant, "IS_LINUX", False):
            for route in (
                handler._api_post_api_v2_launch_grant,
                handler._api_post_api_v2_launch_grant_undo,
            ):
                with self.subTest(route=route.__name__), self.assertRaises(BadRequest) as raised:
                    route({"app_id": "org.libretro.RetroArch", "folder": "/roms"})
                self.assertIn("only available on Linux", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
