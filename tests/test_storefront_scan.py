#!/usr/bin/env python3
"""Storefront scan worker tests, including plugin-provided stores (P5.2/P10.1)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from pkg.state import imports  # noqa: E402


class StorefrontScanTests(unittest.TestCase):
    def _state(self):
        return {
            "settings": {},
            "games": [{
                "game_id": "plugin:vendor.store:one",
                "name": "One",
                "plugin_source": "vendor.store",
                "store_installed": False,
                "store_catalog": True,
            }],
        }

    def test_plugin_source_helpers(self):
        self.assertEqual(imports.plugin_storefront_id("plugin:vendor.store"), "vendor.store")
        self.assertEqual(imports.plugin_storefront_id("plugin:"), "")
        self.assertEqual(imports.plugin_storefront_id("steam"), "")
        with mock.patch.object(
            imports, "storefront_plugin_map", return_value={"plugin:a.b": {"plugin_id": "a.b"}}
        ):
            self.assertEqual(
                imports.storefront_source_ids(Path("/tmp/plugins")),
                (*imports.STOREFRONT_SOURCES, "plugin:a.b"),
            )

    def test_scan_updates_plugin_installed_state(self):
        state = self._state()
        fresh = {
            "game_id": "plugin:vendor.store:one",
            "name": "One",
            "plugin_source": "vendor.store",
            "store_installed": True,
            "path": "/opt/vendor/one",
            "launch": "vendor one",
        }
        written = []
        with mock.patch.object(imports, "DATA", Path("/tmp/openbox-data")), \
             mock.patch.object(imports, "load_state", return_value=state), \
             mock.patch.object(imports, "storefront_plugin_map", return_value={
                 "plugin:vendor.store": {"plugin_id": "vendor.store", "name": "Vendor Store"},
             }), \
             mock.patch.object(imports, "_call_with_timeout", return_value={"games": [fresh]}) as call, \
             mock.patch.object(imports, "update_state", side_effect=lambda mutate: mutate(state)), \
             mock.patch.object(imports, "_write_storefront_status", side_effect=written.append):
            result = imports.storefront_scan_worker(sources=["plugin:vendor.store"])

        game = state["games"][0]
        self.assertTrue(game["store_installed"])
        self.assertEqual(game["path"], "/opt/vendor/one")
        self.assertEqual(result["sources"][0]["name"], "Vendor Store")
        self.assertEqual(result["sources"][0]["status"], "ok")
        self.assertEqual(result["sources"][0]["games"], 1)
        self.assertEqual(written[0][0]["id"], "plugin:vendor.store")
        args = call.call_args.args
        self.assertEqual(args[1][0], "plugin:vendor.store")
        self.assertTrue(args[1][2])

    def test_scan_passes_installed_only_to_plugin_catalog(self):
        with mock.patch.object(imports, "DATA", Path("/tmp/openbox-data")), \
             mock.patch.object(imports, "load_state", return_value={"settings": {}, "games": []}), \
             mock.patch.object(imports, "storefront_plugin_map", return_value={
                 "plugin:vendor.store": {"plugin_id": "vendor.store", "name": "Vendor Store"},
             }), \
             mock.patch.object(imports, "_call_with_timeout", return_value={"games": []}) as call, \
             mock.patch.object(imports, "_write_storefront_status"):
            result = imports.storefront_scan_worker(
                sources=["plugin:vendor.store"], include_uninstalled=False
            )
        self.assertFalse(call.call_args.args[1][2])
        self.assertEqual(result["sources"][0]["status"], "skipped")

    def test_scan_rejects_unknown_plugin_source(self):
        with mock.patch.object(imports, "DATA", Path("/tmp/openbox-data")), \
             mock.patch.object(imports, "load_state", return_value={"settings": {}, "games": []}), \
             mock.patch.object(imports, "storefront_plugin_map", return_value={}):
            with self.assertRaises(ValueError):
                imports.storefront_scan_worker(sources=["plugin:missing.store"])

    def test_scan_route_accepts_enabled_plugin_source(self):
        from api_errors import BadRequest
        from handlers.imports import ImportsHandlers

        handler = object.__new__(ImportsHandlers)
        handler.send_json = mock.Mock()
        with mock.patch(
            "handlers.imports.storefront_source_ids",
            return_value=(*imports.STOREFRONT_SOURCES, "plugin:vendor.store"),
        ), mock.patch("handlers.imports.JOB_MANAGER") as jobs:
            jobs.submit.return_value = {"job_id": "job-1"}
            handler._api_post_api_v2_storefront_scan(
                {"sources": ["plugin:vendor.store"], "include_uninstalled": False}
            )
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_storefront_scan({"sources": ["plugin:missing.store"]})
        handler.send_json.assert_called_once()
        self.assertEqual(handler.send_json.call_args.args[1]["sources"], ["plugin:vendor.store"])


if __name__ == "__main__":
    unittest.main()
