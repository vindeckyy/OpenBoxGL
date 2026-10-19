#!/usr/bin/env python3
"""P9.1 capability manifest validation and additive metadata tests."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins  # noqa: E402


def _package(root, plugin_id="cap.plugin", extra=None):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    manifest = {
        "id": plugin_id,
        "name": "Capability Plugin",
        "version": "1.0.0",
        "api_version": 1,
        "hooks": [],
    }
    manifest.update(extra or {})
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "plugin.py").write_text("def hook(payload):\n    return payload\n", encoding="utf-8")
    return package


def _expect_value_error(callable_object):
    try:
        callable_object()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test():
    # HOOKS expands additively and keeps every v1 hook.
    assert {"before_launch", "after_session", "library", "command", "library_source", "events"} <= plugins.HOOKS
    assert set(plugins.CAPABILITY_HOOKS.values()) <= plugins.HOOKS
    assert "services" not in plugins.HOOKS
    assert plugins.CAPABILITY_HOOKS["emulator_settings"] == "emulator"
    assert plugins.HOOK_TIMEOUTS == {
        "storefront": 30, "metadata": 15, "media": 15, "stats": 15,
        "emulator": 10, "discovery": 10, "default": 5,
    }

    with TemporaryDirectory() as directory:
        package = _package(directory, extra={
            "hooks": ["storefront", "metadata", "media", "stats", "emulator", "discovery"],
            "capabilities": {
                "storefront": {"id": "vendor.store", "name": "Vendor", "platform": "PC"},
                "metadata": {"id": "vendor.meta", "name": "Vendor Metadata", "platform": "Switch"},
                "media": {"id": "vendor.media", "name": "Vendor Media", "media_kinds": ["cover", "video_snap"]},
                "stats": {"id": "vendor.stats", "name": "Vendor Stats"},
                "discovery": {"id": "vendor.discovery", "name": "Vendor Discovery"},
                "emulator_settings": [{
                    "emulator_id": "vendor.emu",
                    "label": "Vendor Emulator",
                    "schema": {"type": "object", "properties": {"fast": {"type": "boolean", "default": True}}},
                    "scopes": ["global", "platform"],
                }],
            },
            "description": "A test plugin",
            "author": "Tests",
            "homepage": "https://example.com/plugin",
            "messages": {
                "ready": {"en": "Ready", "es": "Listo", "xx": "ignored"},
            },
        })
        with mock.patch.object(plugins, "_capability_media_kinds", return_value=frozenset({"cover", "video_snap"})):
            manifest = plugins.read_manifest(package)
        capabilities = manifest["capabilities"]
        assert capabilities["storefront"] == {"id": "vendor.store", "name": "Vendor", "platform": "PC"}
        assert capabilities["metadata"] == {"id": "vendor.meta", "name": "Vendor Metadata", "platform": "Switch"}
        assert capabilities["media"]["media_kinds"] == ["cover", "video_snap"]
        assert capabilities["emulator_settings"][0]["emulator_id"] == "vendor.emu"
        assert capabilities["emulator_settings"][0]["scopes"] == ["global", "platform"]
        assert manifest["description"] == "A test plugin"
        assert manifest["author"] == "Tests"
        assert manifest["homepage"] == "https://example.com/plugin"
        assert manifest["messages"] == {"ready": {"en": "Ready", "es": "Listo"}}
        # list_plugins exposes the same additive fields.
        listed = plugins.list_plugins(directory)[0]
        assert listed["capabilities"]["stats"]["id"] == "vendor.stats"
        assert listed["messages"]["ready"]["en"] == "Ready"

        # Unknown capability keys are ignored for forward compatibility.
        forward = _package(directory, "forward.plugin", {
            "hooks": ["metadata"],
            "capabilities": {"metadata": {"id": "f.meta", "name": "F"}, "future": {"x": 1}},
        })
        with mock.patch.object(plugins, "_capability_media_kinds", return_value=frozenset({"cover", "background"})):
            assert set(plugins.read_manifest(forward)["capabilities"]) == {"metadata"}

        # A capability without its hook is refused.
        missing_hook = _package(directory, "nohook.plugin", {
            "hooks": [],
            "capabilities": {"stats": {"id": "st", "name": "Stats"}},
        })
        _expect_value_error(lambda: plugins.read_manifest(missing_hook))

        # Invalid ids, duplicates, and names.
        for extra in (
            {"hooks": ["stats"], "capabilities": {"stats": {"id": "Bad ID", "name": "x"}}},
            {"hooks": ["stats"], "capabilities": {"stats": {"id": "ok", "name": ""}}},
        ):
            bad = _package(directory, f"bad-{abs(hash(json.dumps(extra)))}.plugin", extra)
            _expect_value_error(lambda bad=bad: plugins.read_manifest(bad))

        duplicate = _package(directory, "dup.plugin", {
            "hooks": ["stats", "discovery"],
            "capabilities": {
                "stats": {"id": "same", "name": "Stats"},
                "discovery": {"id": "same", "name": "Discovery"},
            },
        })
        _expect_value_error(lambda: plugins.read_manifest(duplicate))

        # Media kinds are an allowlist and bounded.
        with mock.patch.object(plugins, "_capability_media_kinds", return_value=frozenset({"cover"})):
            unknown_kind = _package(directory, "kind.plugin", {
                "hooks": ["media"],
                "capabilities": {"media": {"id": "me", "name": "M", "media_kinds": ["hologram"]}},
            })
            _expect_value_error(lambda: plugins.read_manifest(unknown_kind))

        # Emulator capability rules.
        emulator_cases = [
            {"emulator_settings": "nope"},
            {"emulator_settings": [
                {"emulator_id": "e", "schema": {"properties": {}}},
            ] * 9},
            {"emulator_settings": [{"emulator_id": "BAD ID", "schema": {"properties": {}}}]},
            {"emulator_settings": [{"emulator_id": "e"}]},
            {"emulator_settings": [{"emulator_id": "e", "schema": {"properties": {}}, "scopes": ["galaxy"]}]},
            {"emulator_settings": [
                {"emulator_id": "e", "schema": {"properties": {}}},
                {"emulator_id": "e", "schema": {"properties": {}}},
            ]},
        ]
        for index, extra in enumerate(emulator_cases):
            case = _package(directory, f"emu-{index}.plugin", {"hooks": ["emulator"], "capabilities": extra})
            _expect_value_error(lambda case=case: plugins.read_manifest(case))

        # Messages: bounded, named keys, text translations.
        for extra in (
            {"messages": "nope"},
            {"messages": {"Bad Key": {"en": "x"}}},
            {"messages": {"ok": "x"}},
            {"messages": {"ok": {"en": 5}}},
            {"messages": {f"k{i}": {"en": "x"} for i in range(65)}},
        ):
            case = _package(directory, f"msg-{abs(hash(json.dumps(extra)))}.plugin", extra)
            _expect_value_error(lambda case=case: plugins.read_manifest(case))

        # Homepage must be HTTPS.
        insecure = _package(directory, "home.plugin", {"homepage": "http://example.com"})
        _expect_value_error(lambda: plugins.read_manifest(insecure))

    print("plugin capabilities: ok")


if __name__ == "__main__":
    test()
