#!/usr/bin/env python3
"""Plugin discovery capability tests (P10.5)."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from plugins import set_plugin_enabled  # noqa: E402
from pkg.parity import parity_plugin_discovery as discovery  # noqa: E402


def _make_plugin(root, *, plugin_id="vendor.discover", hooks=("discovery",)):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    (package / "plugin.py").write_text("pass\n", encoding="utf-8")
    manifest = {
        "id": plugin_id,
        "name": "Vendor Discovery",
        "version": "1.0.0",
        "api_version": 1,
        "entry": "plugin.py",
        "hooks": list(hooks),
        "capabilities": {"discovery": {"id": "vendor.discovery", "name": "Vendor Discovery"}},
    }
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Path(root)


def _hook(result, calls=None):
    def runner(directory, plugin_id, hook, payload, *, timeout=None):
        if calls is not None:
            calls.append((hook, payload))
        if isinstance(result, Exception):
            raise result
        return result, ""
    return runner


def _games():
    return [
        {"game_id": "g1", "name": "One"},
        {"game_id": "g2", "name": "Two"},
        {"game_id": "g3", "name": "Three"},
    ]


def test_lists_resolve_to_indices():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        calls = []
        result = {"lists": [
            {"id": "recent", "name": "Recent", "game_ids": ["g1", "zzz", "g1", "g2"]},
            {"id": "empty", "name": "Empty", "game_ids": []},
            {"id": "unknown", "name": "Unknown", "game_ids": ["zzz"]},
            {"id": "bad"},
            "not-a-list",
            {"id": "all", "name": "All", "game_ids": ["g3", "g1"]},
        ]}
        with mock.patch.object(discovery, "run_plugin_hook", _hook(result, calls)):
            payload = discovery.fetch_plugin_discovery(
                "vendor.discover", _games(), limit=10, plugins_dir=root)
        assert calls[0][1] == {"limit": 10}
        key = "plugin:vendor.discover:recent"
        assert payload["lists"][key] == [0, 1]
        assert payload["game_ids"][key] == ["g1", "g2"]
        assert payload["names"][key] == "Recent"
        assert payload["lists"]["plugin:vendor.discover:all"] == [2, 0]
        assert "plugin:vendor.discover:empty" not in payload["lists"]
        assert "plugin:vendor.discover:unknown" not in payload["lists"]
        assert payload["truncated"] is False


def test_caps_lists_and_ids():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        lists = [
            {"id": f"list{index}", "name": f"List {index}",
             "game_ids": [f"g{(index + offset) % 3 + 1}" for offset in range(60)]}
            for index in range(12)
        ]
        with mock.patch.object(discovery, "run_plugin_hook", _hook({"lists": lists})):
            payload = discovery.fetch_plugin_discovery(
                "vendor.discover", _games(), limit=50, plugins_dir=root)
        assert len(payload["lists"]) == discovery.MAX_LISTS
        assert payload["truncated"] is True
        assert all(len(ids) <= 3 for ids in payload["game_ids"].values())
        assert list(payload["lists"])[0] == "plugin:vendor.discover:list0"


def test_hook_failure_returns_empty_shape():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        with mock.patch.object(discovery, "run_plugin_hook", lambda *a, **k: (None, "boom")):
            payload = discovery.fetch_plugin_discovery("vendor.discover", _games(), plugins_dir=root)
        assert payload["lists"] == {}
        assert payload["names"] == {}
        assert payload["game_ids"] == {}
        assert "boom" in payload["error"]

        with mock.patch.object(discovery, "run_plugin_hook", _hook(ValueError("crash"))):
            payload = discovery.fetch_plugin_discovery("vendor.discover", _games(), plugins_dir=root)
        assert payload["lists"] == {}
        assert "crash" in payload["error"]

        with mock.patch.object(discovery, "run_plugin_hook", lambda *a, **k: ({"lists": "no"}, "")):
            payload = discovery.fetch_plugin_discovery("vendor.discover", _games(), plugins_dir=root)
        assert payload["lists"] == {}
        assert "lists" in payload["error"]


def test_merge_adds_plugin_lists_without_clobbering_builtins():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        result = {"lists": [
            {"id": "recent", "name": "Plugin recent", "game_ids": ["g2", "zzz", "g2"]},
        ]}
        with mock.patch.object(discovery, "run_plugin_hook", _hook(result)):
            payload = discovery.merge_plugin_discovery(
                {"recently_added": [0]}, _games(), limit=10, plugins_dir=root)
        key = "plugin:vendor.discover:recent"
        assert payload["recently_added"] == [0]
        assert payload[key] == [1]
        assert payload["discovery_names"][key] == "Plugin recent"


def test_disabled_plugin_rejected():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_enabled(root, "vendor.discover", False)
        try:
            discovery.fetch_plugin_discovery("vendor.discover", _games(), plugins_dir=root)
            raise AssertionError("disabled plugin must be refused")
        except ValueError as error:
            assert "disabled" in str(error).lower()


def test_missing_capability_rejected():
    with TemporaryDirectory() as directory:
        root = Path(directory) / "plugins"
        package = root / "vendor.discover"
        package.mkdir(parents=True)
        (package / "plugin.py").write_text("pass\n", encoding="utf-8")
        (package / "plugin.json").write_text(json.dumps({
            "id": "vendor.discover", "name": "Vendor", "version": "1",
            "hooks": ["discovery"],
        }), encoding="utf-8")
        try:
            discovery.fetch_plugin_discovery("vendor.discover", _games(), plugins_dir=root)
            raise AssertionError("capability must be required")
        except ValueError as error:
            assert "capability" in str(error).lower()


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"ok {name}")
    print("test_plugin_discovery.py OK")
