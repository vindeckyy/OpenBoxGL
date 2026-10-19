#!/usr/bin/env python3
"""P9.2 plugin data/credential storage, payload injection, and results."""

import json
import os
import stat
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
        "name": "Storage Plugin",
        "version": "1.0.0",
        "hooks": ["events", "stats", "media", "before_launch"],
        "settings": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "default": "fast"},
                "api_key": {"type": "string", "format": "password"},
            },
        },
    }
    manifest.update(extra or {})
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "plugin.py").write_text("def events(p):\n    return p\n", encoding="utf-8")
    return package


def _mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def test():
    with TemporaryDirectory() as directory:
        root = Path(directory) / "plugins"
        package = _package(root, "store.plugin")
        plugins.set_plugin_settings(root, "store.plugin", {"mode": "slow", "api_key": "top-secret"})

        # Data bag: merge, bounds, and 0600 storage.
        assert plugins.plugin_data_values(root, "store.plugin") == {}
        assert plugins.set_plugin_data(root, "store.plugin", {"seen": 2, "nested": {"a": 1}}) == {
            "seen": 2, "nested": {"a": 1},
        }
        data_file = root / "data" / "store.plugin.json"
        if os.name != "nt":
            assert _mode(data_file) == 0o600
        assert len(plugins.plugin_data_values(root, "store.plugin")) == 2
        assert plugins.set_plugin_data(root, "store.plugin", {"seen": 3})["seen"] == 3
        for bad in (
            {"x" * 129: 1},
            {"deep": {"a": {"b": {"c": {"d": 1}}}}},
            {"oversized": "x" * (plugins.MAX_PLUGIN_DATA + 1)},
            {"bad": object()},
        ):
            try:
                plugins.set_plugin_data(root, "store.plugin", bad)
                raise AssertionError(f"expected ValueError for {list(bad)[0]}")
            except ValueError:
                pass
        # A malformed or oversized data file reads as empty, never crashes.
        data_file.write_text("{not json", encoding="utf-8")
        assert plugins.plugin_data_values(root, "store.plugin") == {}
        data_file.write_text(json.dumps({"x": "y" * plugins.MAX_PLUGIN_DATA}), encoding="utf-8")
        assert plugins.plugin_data_values(root, "store.plugin") == {}

        # Credentials: merge, delete, clear, redacted summary, 0600.
        assert plugins.set_plugin_credentials(root, "store.plugin", {"token": "abc", "user": "me"}) == ["token", "user"]
        credentials_file = root / "credentials" / "store.plugin.json"
        if os.name != "nt":
            assert _mode(credentials_file) == 0o600
        assert plugins.plugin_credentials_values(root, "store.plugin") == {"token": "abc", "user": "me"}
        plugins.set_plugin_credentials(root, "store.plugin", {"token": ""})
        assert plugins.plugin_credentials_values(root, "store.plugin") == {"user": "me"}
        plugins.set_plugin_credentials(root, "store.plugin", {}, clear=["user"])
        assert plugins.plugin_credentials_values(root, "store.plugin") == {}
        assert not credentials_file.exists()
        assert plugins.plugin_credentials_summary(root, "store.plugin") == {"keys": [], "set": {}}
        plugins.set_plugin_credentials(root, "store.plugin", {"token": "abc"})
        assert plugins.plugin_credentials_summary(root, "store.plugin") == {"keys": ["token"], "set": {"token": True}}
        for bad in (
            {"x": "y" * (plugins.MAX_CREDENTIAL_VALUE + 1)},
            {f"k{i}": "v" for i in range(plugins.MAX_CREDENTIAL_KEYS + 1)},
            {"ok": 5},
        ):
            try:
                plugins.set_plugin_credentials(root, "store.plugin", bad)
                raise AssertionError("expected ValueError for credentials")
            except ValueError:
                pass

        # Settings redaction for the UI.
        public = plugins.get_plugin_settings_public(root, "store.plugin")
        assert public["values"] == {"mode": "slow"}
        assert public["secret_set"] == {"api_key": True}
        assert "top-secret" not in json.dumps(public)

        # Capability payloads get data, redacted settings, and credentials;
        # legacy hooks get data plus redacted settings, never credentials.
        manifest = plugins.read_manifest(package)
        capability_payload = plugins._hook_payload(root, manifest, "stats", {"action": "fetch"})
        assert capability_payload["plugin_data"] == {}
        assert capability_payload["settings"] == {"mode": "slow"}
        assert capability_payload["credentials"] == {"token": "abc"}
        legacy_payload = plugins._hook_payload(root, manifest, "before_launch", {"args": []})
        assert legacy_payload["plugin_data"] == {}
        assert legacy_payload["settings"] == {"mode": "slow"}
        assert "credentials" not in legacy_payload

        # Hook results: data merges, credentials are refused, notifications stay.
        plugins.set_plugin_data(root, "store.plugin", {})
        with mock.patch.object(plugins, "_invoke_hook", return_value=(
            {"data": {"counter": 4}, "credentials": {"api_key": "leak"}, "notification": {"message": "hi"}},
            "",
        )):
            result, error = plugins._run_manifest_hook(
                root, {"id": "store.plugin", "entry": "plugin.py"}, "events", {},
            )
        assert error == ""
        assert plugins.plugin_data_values(root, "store.plugin") == {"counter": 4}
        assert "credentials" not in result
        assert result["notification"] == {"message": "hi"}

        # Invalid data is ignored without failing the hook.
        with mock.patch.object(plugins, "_invoke_hook", return_value=({"data": "nope"}, "")):
            result, error = plugins._run_manifest_hook(
                root, {"id": "store.plugin", "entry": "plugin.py"}, "events", {},
            )
        assert error == "" and result == {"data": "nope"}

        # Service requests: one round-trip, first result never merged.
        calls = []

        def fake_invoke(_root, _manifest, _hook, payload, *, timeout=None):
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "service_requests": [{"id": "r1", "action": "profile", "params": {}}],
                    "data": {"must": "not merge"},
                }, ""
            return {"ok": True, "service_requests": [{"id": "r2", "action": "profile", "params": {}}]}, ""

        with mock.patch.object(plugins, "_invoke_hook", side_effect=fake_invoke), \
             mock.patch.object(plugins, "_service_state", return_value={
                 "games": [{"name": "A", "playtime_seconds": 60}],
             }):
            result, error = plugins._run_manifest_hook(
                root, {"id": "store.plugin", "entry": "plugin.py"}, "stats", {"action": "fetch"},
            )
        assert error == "", error
        assert len(calls) == 2
        assert calls[1]["service_results"]["r1"]["games"] == 1
        assert "service_requests" not in result
        assert "must" not in plugins.plugin_data_values(root, "store.plugin")

        # service_requests are rejected outside capability/privileged hooks.
        with mock.patch.object(plugins, "_invoke_hook", return_value=(
            {"service_requests": [{"id": "r", "action": "profile", "params": {}}]}, "",
        )):
            result, error = plugins._run_manifest_hook(
                root, {"id": "store.plugin", "entry": "plugin.py"}, "before_launch", {},
            )
        assert result is None and "capability hooks" in error

        # Removing a plugin clears data and credentials with the package.
        plugins.set_plugin_data(root, "store.plugin", {"counter": 1})
        plugins.set_plugin_credentials(root, "store.plugin", {"token": "abc"})
        stats_file = root.parent / "plugin-stats" / "store.plugin.json"
        stats_file.parent.mkdir(parents=True, exist_ok=True)
        stats_file.write_text("{}", encoding="utf-8")
        plugins.remove_plugin(root, "store.plugin")
        assert plugins.plugin_data_values(root, "store.plugin") == {}
        assert plugins.plugin_credentials_values(root, "store.plugin") == {}
        assert not stats_file.exists()

        # Malformed request containers fail loudly.
        with mock.patch.object(plugins, "_invoke_hook", return_value=(
            {"service_requests": [{"id": "r", "action": "profile", "params": {}}] * 9}, "",
        )):
            result, error = plugins._run_manifest_hook(
                root, {"id": "store.plugin", "entry": "plugin.py"}, "stats", {},
            )
        assert result is None and "at most 8" in error

        # An unknown action is recorded per request; the hook still gets one
        # second invocation with the error result.
        service_calls = []

        def fake_service_invoke(_root, _manifest, _hook, payload, *, timeout=None):
            service_calls.append(payload)
            if len(service_calls) == 1:
                return {"service_requests": [{"id": "r", "action": "nope", "params": {}}]}, ""
            return {"ok": True}, ""

        with mock.patch.object(plugins, "_invoke_hook", side_effect=fake_service_invoke), \
             mock.patch.object(plugins, "_service_state", return_value={}):
            result, error = plugins._run_manifest_hook(
                root, {"id": "store.plugin", "entry": "plugin.py"}, "stats", {},
            )
        assert error == "" and result == {"ok": True}
        assert "Unknown plugin service" in service_calls[1]["service_results"]["r"]["error"]

        # Hook timeouts resolve through HOOK_TIMEOUTS and honour overrides.
        recorded = []

        def fake_run(_command, **kwargs):
            recorded.append(kwargs["timeout"])
            return SimpleNamespace(returncode=0)

        manifest = {"id": "store.plugin", "entry": "plugin.py"}
        with mock.patch.object(plugins, "_plugin_command", return_value=["true"]), \
             mock.patch.object(plugins.subprocess, "run", side_effect=fake_run), \
             mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            plugins._invoke_hook(root, manifest, "storefront", {})
            plugins._invoke_hook(root, manifest, "before_launch", {})
            plugins._invoke_hook(root, manifest, "stats", {}, timeout=7)
        assert recorded == [30, 5, 7]

    print("plugin storage: ok")


if __name__ == "__main__":
    test()
