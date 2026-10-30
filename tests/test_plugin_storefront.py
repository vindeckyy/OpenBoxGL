#!/usr/bin/env python3
"""Plugin storefront capability tests (P10.1)."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins import set_plugin_enabled, set_plugin_permissions, set_plugin_settings  # noqa: E402
from pkg.parity import parity_plugin_storefront as store  # noqa: E402


def _make_plugin(root, *, plugin_id="vendor.store", hooks=("storefront",), permissions=("network",),
                 capabilities=None, settings=None):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    entry = package / "plugin.py"
    entry.write_text("def storefront(payload):\n    return {}\n", encoding="utf-8")
    manifest = {
        "id": plugin_id,
        "name": "Vendor Store",
        "version": "1.0.0",
        "api_version": 1,
        "entry": "plugin.py",
        "hooks": list(hooks),
        "permissions": list(permissions),
        "capabilities": capabilities or {
            "storefront": {"id": "vendor.storefront", "name": "Vendor Store"},
        },
    }
    if settings is not None:
        manifest["settings"] = settings
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Path(root)


def _catalog_hook(pages, calls=None):
    def runner(directory, plugin_id, hook, payload, *, timeout=None):
        if calls is not None:
            calls.append((hook, payload, timeout))
        page = pages.get(payload.get("cursor", ""))
        if page is None:
            return {}, ""
        if isinstance(page, Exception):
            raise page
        return page, ""
    return runner


def test_list_storefront_plugins():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins", settings={
            "type": "object",
            "properties": {
                "region": {"type": "string", "default": "us"},
                "api_key": {"type": "string", "format": "password"},
            },
        })
        set_plugin_permissions(root, "vendor.store", ["network"])
        set_plugin_settings(root, "vendor.store", {"region": "eu", "api_key": "secret"})
        items = store.list_storefront_plugins(root)
        assert len(items) == 1
        item = items[0]
        assert item["plugin_id"] == "vendor.store"
        assert item["id"] == "vendor.storefront"
        assert item["name"] == "Vendor Store"
        assert item["permission_granted"] is True
        assert item["settings_values"] == {"region": "eu"}
        assert "api_key" not in item["settings_values"]

        set_plugin_permissions(root, "vendor.store", [])
        assert store.list_storefront_plugins(root)[0]["permission_granted"] is False

        set_plugin_enabled(root, "vendor.store", False)
        assert store.list_storefront_plugins(root) == []
        try:
            store.plugin_storefront_catalog("vendor.store", {"region": "eu"}, plugins_dir=root)
            raise AssertionError("disabled plugin must be refused")
        except ValueError as error:
            assert "disabled" in str(error).lower()


def test_catalog_paginates_and_validates_entries():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.store", ["network"])
        pages = {
            "": {
                "entries": [
                    {"id": "one", "name": "One", "installed": True,
                     "install_uri": "https://store.example/install/1",
                     "repair_uri": "https://store.example/validate/1",
                     "path": "/usr/bin/store", "launch": "store 1"},
                    {"id": "two", "name": "Two", "installed": False,
                     "install_uri": "http://store.example/install/2"},
                    {"id": "bad"},  # missing name
                    "not-an-object",
                ],
                "next_cursor": "page-2",
            },
            "page-2": {
                "entries": [{"id": "three", "name": "Three", "installed": True}],
                "next_cursor": "",
            },
        }
        calls = []
        with mock.patch.object(store, "run_plugin_hook", _catalog_hook(pages, calls)):
            entries = store.plugin_storefront_catalog(
                "vendor.store", {"region": "eu"}, installed_only=True, timeout=12,
                plugins_dir=root,
            )
        assert [entry["plugin_store_id"] for entry in entries] == ["one", "two", "three"]
        assert entries[0]["id"] == "plugin:vendor.store:one"
        assert entries[0]["source"] == "Vendor Store"
        assert entries[0]["store_catalog"] is True
        assert entries[0]["install_uri"] == "https://store.example/install/1"
        assert entries[1]["install_uri"] == ""
        assert len(calls) == 2
        assert calls[0][1]["installed_only"] is True
        assert calls[0][2] == 12
        assert calls[1][1]["cursor"] == "page-2"


def test_catalog_repeated_cursor_and_malformed_page():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.store", ["network"])
        looping = {
            "": {"entries": [{"id": "a", "name": "A"}], "next_cursor": "same"},
            "same": {"entries": [{"id": "b", "name": "B"}], "next_cursor": "same"},
        }
        with mock.patch.object(store, "run_plugin_hook", _catalog_hook(looping)):
            try:
                store.plugin_storefront_catalog("vendor.store", {}, plugins_dir=root)
                raise AssertionError("repeated cursor must be refused")
            except ValueError as error:
                assert "cursor" in str(error).lower()

        with mock.patch.object(store, "run_plugin_hook",
                               lambda *args, **kwargs: ({"next_cursor": ""}, "")):
            try:
                store.plugin_storefront_catalog("vendor.store", {}, plugins_dir=root)
                raise AssertionError("missing entries must be refused")
            except ValueError as error:
                assert "entries" in str(error).lower()

        with mock.patch.object(store, "run_plugin_hook",
                               lambda *args, **kwargs: (None, "boom")):
            try:
                store.plugin_storefront_catalog("vendor.store", {}, plugins_dir=root)
                raise AssertionError("hook error must be surfaced")
            except ValueError as error:
                assert "boom" in str(error)


def test_catalog_requires_network_grant():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        try:
            store.plugin_storefront_catalog("vendor.store", {}, plugins_dir=root)
            raise AssertionError("missing network grant must be refused")
        except ValueError as error:
            assert "grant network" in str(error).lower()


def test_import_filters_and_namespaces():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.store", ["network"])
        pages = {
            "": {
                "entries": [
                    {"id": "installed", "name": "Installed Game", "installed": True,
                     "platform": "PC", "path": "/usr/bin/store", "launch": "store run"},
                    {"id": "missing", "name": "Missing Game", "installed": False,
                     "platform": "PC", "path": "/usr/bin/store",
                     "install_uri": "https://store.example/install/missing"},
                ],
                "next_cursor": "",
            },
        }
        import_calls = []
        with mock.patch.object(store, "run_plugin_hook", _catalog_hook(pages, import_calls)):
            games = store.plugin_storefront_import("vendor.store", {}, plugins_dir=root)
        assert import_calls[0][1]["action"] == "import"
        assert len(games) == 2
        assert games[0]["game_id"] == "plugin:vendor.store:installed"
        assert games[0]["plugin_source"] == "vendor.store"
        assert games[0]["plugin_source_name"] == "Vendor Store"
        assert games[0]["store_installed"] is True
        assert games[0]["store_catalog"] is True
        assert games[0]["owned"] is True
        assert "id" not in games[0]
        assert games[1]["store_installed"] is False
        assert games[1]["install_uri"] == "https://store.example/install/missing"
        assert "Owned via Vendor Store" in games[1]["notes"]

        with mock.patch.object(store, "run_plugin_hook", _catalog_hook(pages)):
            installed = store.plugin_storefront_import(
                "vendor.store", {}, installed_only=True, plugins_dir=root)
        assert [game["name"] for game in installed] == ["Installed Game"]

        with mock.patch.object(store, "run_plugin_hook", _catalog_hook(pages)):
            missing = store.plugin_storefront_import(
                "vendor.store", {}, uninstalled_only=True, plugins_dir=root)
        assert [game["name"] for game in missing] == ["Missing Game"]


def test_import_accepts_games_collection():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.store", ["network"])
        pages = {
            "": {
                "games": [
                    {"game_id": "imported", "name": "Imported Game", "platform": "PC",
                     "path": "/usr/bin/store"},
                ],
                "next_cursor": "",
            },
        }
        with mock.patch.object(store, "run_plugin_hook", _catalog_hook(pages)):
            games = store.plugin_storefront_import("vendor.store", {}, plugins_dir=root)
        assert [game["game_id"] for game in games] == ["plugin:vendor.store:imported"]
        assert games[0]["name"] == "Imported Game"


def test_missing_capability_rejected():
    with TemporaryDirectory() as directory:
        root = _make_plugin(
            Path(directory) / "plugins",
            hooks=("command",),
            permissions=(),
            capabilities={},
        )
        try:
            store.plugin_storefront_catalog("vendor.store", {}, plugins_dir=root)
            raise AssertionError("storefront capability missing must be refused")
        except ValueError as error:
            assert "capability" in str(error).lower() or "manifest" in str(error).lower()


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"ok {name}")
    print("test_plugin_storefront.py OK")
