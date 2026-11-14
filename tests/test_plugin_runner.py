#!/usr/bin/env python3
"""Plugin runner contract: v1 is gone, v2 still times out a hung hook."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins import (  # noqa: E402
    PLUGIN_API_RETIRED,
    PluginApiRetired,
    _plugin_command,
    read_manifest,
    run_plugin_hook,
)


def _package(root, plugin_id, manifest, source):
    package = root / plugin_id
    package.mkdir()
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "plugin.py").write_text(source, encoding="utf-8")
    return package


def test():
    assert not (ROOT / "plugin_runner.py").exists()
    assert (ROOT / "plugin_runner_v2.py").is_file()

    source = (
        "def before_launch(payload):\n"
        "    payload['args'].append('--ran')\n"
        "    return payload\n"
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        omitted = _package(root, "omitted.plugin", {
            "id": "omitted.plugin", "name": "Omitted", "version": "1",
            "entry": "plugin.py", "hooks": ["before_launch"],
        }, source)
        try:
            read_manifest(omitted)
            raise AssertionError("omitted api_version was accepted")
        except PluginApiRetired as error:
            assert error.code == PLUGIN_API_RETIRED
            assert "must be 2" in str(error)

        explicit = _package(root, "v1.plugin", {
            "id": "v1.plugin", "name": "V1", "version": "1", "api_version": 1,
            "entry": "plugin.py", "hooks": ["before_launch"],
        }, source)
        try:
            read_manifest(explicit)
            raise AssertionError("api_version 1 was accepted")
        except PluginApiRetired as error:
            assert error.code == PLUGIN_API_RETIRED

        current = _package(root, "v2.plugin", {
            "id": "v2.plugin", "name": "V2", "version": "1", "api_version": 2,
            "entry": "plugin.py", "hooks": ["before_launch"],
        }, source)
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            command = _plugin_command(current, current / "plugin.py", "before_launch")
        joined = " ".join(command)
        assert "plugin_runner.py" not in joined, joined
        assert "plugin_runner_v2.py" in joined
        assert "before_launch" not in command

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _package(root, "slow.plugin", {
            "id": "slow.plugin", "name": "Slow", "version": "1", "api_version": 2,
            "entry": "plugin.py", "hooks": ["before_launch"],
        }, "import time\ndef before_launch(payload):\n    time.sleep(30)\n    return payload\n")
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            result, error = run_plugin_hook(root, "slow.plugin", "before_launch", {"args": []})
        assert result is None
        assert "timed out" in error.lower() or "timeout" in error.lower(), error
    print("plugin runner self-test: ok")


if __name__ == "__main__":
    test()
