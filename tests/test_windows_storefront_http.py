"""Route-level tests for the native Windows storefront import routes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest  # noqa: E402
import handlers.imports as imports_handler  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))

    def _import_windows_store(self, discover, source, id_field):
        return imports_handler.ImportsHandlers._import_windows_store(
            self, discover, source, id_field
        )


class WindowsStoreHttpTests(unittest.TestCase):
    def setUp(self):
        self.state = {"games": [], "settings": {}}

        def fake_update(mutator):
            return mutator(self.state), None

        self.patch = mock.patch.object(imports_handler, "merge_imported_games", imports_handler.merge_imported_games)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.update_patch = mock.patch("pkg.state.imports.update_state", side_effect=fake_update)
        self.update_patch.start()
        self.addCleanup(self.update_patch.stop)
        self.features_patch = mock.patch.object(
            imports_handler, "platform_features", return_value={"native_storefronts": True}
        )
        self.features_patch.start()
        self.addCleanup(self.features_patch.stop)

    def game(self):
        return {
            "name": "Test Game",
            "platform": "PC",
            "source": "GOG",
            "collection": "GOG",
            "path": "/games/Test Game/Game.exe",
            "launch": "",
            "gog_id": "123",
            "install_dir": "/games/Test Game",
        }

    def test_import_then_reimport_adds_nothing(self):
        handler = Handler()
        handler._import_windows_store(lambda *, errors=None: [self.game()], "gog", "gog_id")
        self.assertEqual(handler.responses[-1][1]["added"], 1)
        handler._import_windows_store(lambda *, errors=None: [self.game()], "gog", "gog_id")
        self.assertEqual(handler.responses[-1][1]["added"], 0)
        self.assertEqual(handler.responses[-1][1]["found"], 1)

    def test_errors_are_surfaced(self):
        def discover(*, errors=None):
            errors.append("missing install dir")
            return []

        handler = Handler()
        handler._import_windows_store(discover, "gog", "gog_id")
        self.assertEqual(handler.responses[-1][1]["errors"], ["missing install dir"])

    def test_routes_reject_non_objects(self):
        instance = imports_handler.ImportsHandlers()
        for method in (
            instance._api_post_api_v2_import_gog,
            instance._api_post_api_v2_import_ea,
            instance._api_post_api_v2_import_ubisoft,
        ):
            with self.assertRaises(BadRequest):
                method([])

    def test_routes_accept_objects_and_call_their_importer(self):
        instance = imports_handler.ImportsHandlers()
        for route, name in (
            (instance._api_post_api_v2_import_gog, "import_gog_games"),
            (instance._api_post_api_v2_import_ea, "import_ea_games"),
            (instance._api_post_api_v2_import_ubisoft, "import_ubisoft_games"),
        ):
            with mock.patch.object(instance, name) as called:
                route({})
                called.assert_called_once()

    def test_named_import_methods_run_their_discoverer(self):
        for method, patch_name in (
            ("import_gog_games", "import_gog_windows"),
            ("import_ea_games", "import_ea_windows"),
            ("import_ubisoft_games", "import_ubisoft_windows"),
        ):
            handler = Handler()
            with mock.patch.object(imports_handler, patch_name, return_value=[]):
                getattr(imports_handler.ImportsHandlers, method)(handler)
            self.assertEqual(handler.responses[-1][1]["added"], 0)

    def test_the_windows_importers_refuse_other_hosts(self):
        discover = mock.Mock(return_value=[])
        handler = Handler()
        with mock.patch.object(
            imports_handler, "platform_features", return_value={"native_storefronts": False}
        ):
            for method, patch_name in (
                ("import_gog_games", "import_gog_windows"),
                ("import_ea_games", "import_ea_windows"),
                ("import_ubisoft_games", "import_ubisoft_windows"),
            ):
                with self.subTest(method=method), mock.patch.object(imports_handler, patch_name, discover):
                    with self.assertRaises(BadRequest):
                        getattr(imports_handler.ImportsHandlers, method)(handler)
        discover.assert_not_called()

    def test_the_storefront_list_follows_the_capability_contract(self):
        instance = imports_handler.ImportsHandlers()
        sent = []
        instance.send_json = lambda status, payload: sent.append(payload)
        with mock.patch.object(imports_handler, "list_storefront_plugins", return_value=[]):
            for features, expected in (
                ({"heroic": False, "lutris": False, "native_storefronts": False}, ["steam", "gameyfin"]),
                ({"heroic": True, "lutris": True, "native_storefronts": False}, ["steam", "gameyfin", "heroic", "lutris"]),
                ({"heroic": True, "lutris": False, "native_storefronts": True}, ["steam", "gameyfin", "heroic", "gog", "ea", "ubisoft"]),
            ):
                with self.subTest(features=features), mock.patch.object(
                    imports_handler, "platform_features", return_value=features
                ):
                    instance._api_get_api_v2_storefronts({})
                self.assertEqual([item["id"] for item in sent[-1]["storefronts"]], expected)


if __name__ == "__main__":
    unittest.main()
