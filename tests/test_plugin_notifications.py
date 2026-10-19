#!/usr/bin/env python3
"""P9.3 localized notifications, event persistence, and hook attribution."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins  # noqa: E402


def _package(root, plugin_id, extra=None):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id,
        "version": "1.0.0",
        "hooks": ["events"],
    }
    manifest.update(extra or {})
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "plugin.py").write_text("def events(p):\n    return p\n", encoding="utf-8")
    return package


def test_resolution():
    manifest = {
        "messages": {
            "ready": {"en": "Ready, {name}", "es": "Listo, {name}"},
            "plain": {"en": "No params"},
        },
    }
    assert plugins.resolve_plugin_notification(
        manifest, {"key": "ready", "params": {"name": "Ada"}}, locale="es",
    ) == {"level": "info", "message": "Listo, Ada"}
    # Locale falls back to English, then the key itself.
    assert plugins.resolve_plugin_notification(
        manifest, {"key": "ready", "params": {"name": "Ada"}}, locale="de",
    )["message"] == "Ready, Ada"
    assert plugins.resolve_plugin_notification(
        manifest, {"key": "missing.key"}, locale="es",
    )["message"] == "missing.key"
    # Format failures keep the raw template rather than dropping it.
    assert plugins.resolve_plugin_notification(
        manifest, {"key": "ready"}, locale="en",
    )["message"] == "Ready, {name}"
    assert plugins.resolve_plugin_notification(
        manifest, {"message": "Direct", "level": "warning"},
    ) == {"level": "warning", "message": "Direct"}
    assert plugins.resolve_plugin_notification(
        manifest, {"message": "x", "level": "bogus"},
    )["level"] == "info"
    # Strings are accepted as messages; other shapes are rejected.
    assert plugins.resolve_plugin_notification(manifest, "Plain")["message"] == "Plain"
    try:
        plugins.resolve_plugin_notification(manifest, ["nope"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    long_message = plugins.resolve_plugin_notification(manifest, {"message": "x" * 4000})
    assert len(long_message["message"]) == 2000


def test_persistence():
    state = {"notifications": []}
    fake_openbox = SimpleNamespace(update_state=lambda mutator: mutator(state))
    with mock.patch.dict(sys.modules, {"openbox": fake_openbox}):
        plugins._persist_plugin_notification(
            "notify.plugin", "app_startup", {"level": "success", "message": "Loaded"},
        )
        plugins._persist_plugin_notification(
            "notify.plugin", "app_startup", {"level": "success", "message": "Loaded"},
        )
    items = state["notifications"]
    assert len(items) == 1, items
    item = items[0]
    assert item["kind"] == "plugin" and item["source"] == "notify.plugin"
    assert item["dedupe_key"] == "plugin:notify.plugin:app_startup"
    assert item["title"] == "Loaded" and item["level"] == "success"


def test_event_fanout():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _package(root, "notify.plugin", {
            "messages": {"done": {"en": "Scan done", "es": "Escaneo listo"}},
        })
        _package(root, "silent.plugin")
        manifests = [
            {"id": "notify.plugin", "valid": True, "enabled": True, "hooks": ["events"]},
            {"id": "silent.plugin", "valid": True, "enabled": True, "hooks": ["events"]},
        ]

        def fake_run(_root, manifest, _hook, _payload, *, timeout=None):
            if manifest["id"] == "notify.plugin":
                return {"notification": {"key": "done"}}, ""
            return {"ok": True}, ""

        stored = []
        with mock.patch.object(plugins, "list_plugins", return_value=manifests), \
             mock.patch.object(plugins, "_run_manifest_hook", side_effect=fake_run), \
             mock.patch.object(plugins, "_active_locale", return_value="es"), \
             mock.patch.object(plugins, "_persist_plugin_notification",
                               side_effect=lambda plugin_id, event, resolved: stored.append(
                                   (plugin_id, event, resolved))):
            plugins.emit_plugin_event(root, "scan_finished", {"added": 3})
        assert stored == [("notify.plugin", "scan_finished", {"level": "info", "message": "Escaneo listo"})]

        # Safe mode suppresses everything.
        stored.clear()
        with mock.patch.dict("os.environ", {"OPENBOX_SAFE_MODE": "1"}), \
             mock.patch.object(plugins, "list_plugins", return_value=manifests), \
             mock.patch.object(plugins, "_run_manifest_hook", side_effect=fake_run):
            plugins.emit_plugin_event(root, "scan_finished", {})
        assert stored == []

        # A failure while storing one notification never escapes the event path.
        with mock.patch.object(plugins, "list_plugins", return_value=manifests), \
             mock.patch.object(plugins, "_run_manifest_hook", side_effect=fake_run), \
             mock.patch.object(plugins, "_active_locale", return_value="en"), \
             mock.patch.object(plugins, "_persist_plugin_notification", side_effect=RuntimeError("boom")):
            plugins.emit_plugin_event(root, "scan_finished", {})

        # A plugin that fails is logged, not fatal; no notification is stored.
        broken = [{"id": "broken.plugin", "valid": True, "enabled": True, "hooks": ["events"]}]
        with mock.patch.object(plugins, "list_plugins", return_value=broken), \
             mock.patch.object(plugins, "_run_manifest_hook", return_value=(None, "boom")), \
             mock.patch.object(plugins, "_persist_plugin_notification") as persist:
            plugins.emit_plugin_event(root, "app_startup", {})
        persist.assert_not_called()


def test_attribution_and_chaining():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _package(root, "one.plugin", {"hooks": ["before_launch"]})
        _package(root, "two.plugin", {"hooks": ["before_launch"]})
        calls = []

        def fake_run(_root, manifest, _hook, payload, *, timeout=None):
            calls.append(manifest["id"])
            return ({"args": [manifest["id"]]}, "") if manifest["id"] == "one.plugin" else ({"args": ["two"]}, "")

        with mock.patch.object(plugins, "_run_manifest_hook", side_effect=fake_run):
            results = plugins.run_plugin_hooks(root, "before_launch", {"args": []})
        assert [item[0] for item in results] == ["one.plugin", "two.plugin"]
        assert all(error == "" for _, _, error in results)

        # run_plugins still chains each result into the next plugin.
        seen = []

        def chaining_run(_root, manifest, _hook, payload, *, timeout=None):
            seen.append((manifest["id"], payload["n"]))
            return {"n": payload["n"] + 1}, ""

        manifests = [
            {"id": "one.plugin", "valid": True, "enabled": True, "hooks": ["before_launch"]},
            {"id": "two.plugin", "valid": True, "enabled": True, "hooks": ["before_launch"]},
        ]
        with mock.patch.object(plugins, "list_plugins", return_value=manifests), \
             mock.patch.object(plugins, "_run_manifest_hook", side_effect=chaining_run):
            result = plugins.run_plugins(root, "before_launch", {"n": 0})
        assert result == {"n": 2}
        assert seen == [("one.plugin", 0), ("two.plugin", 1)]

        # Disabled plugins are skipped by the attributed runner.
        manifests[1] = {**manifests[1], "enabled": False}
        with mock.patch.object(plugins, "list_plugins", return_value=manifests), \
             mock.patch.object(plugins, "_run_manifest_hook", side_effect=chaining_run):
            results = plugins.run_plugin_hooks(root, "before_launch", {"n": 0})
        assert [item[0] for item in results] == ["one.plugin"]

    print("plugin notifications: ok")


if __name__ == "__main__":
    test_resolution()
    test_persistence()
    test_event_fanout()
    test_attribution_and_chaining()
