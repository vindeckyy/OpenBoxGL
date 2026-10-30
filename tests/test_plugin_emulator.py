#!/usr/bin/env python3
"""Plugin-declared emulator settings (P10.4)."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.parity.parity_emulator_settings import (  # noqa: E402
    apply_plugin_settings,
    plugin_emulator_sections,
    plugin_section_key,
)


def _make_plugin(root, plugin_id="vendor.cfg", emulator_id="mame"):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(
        "def emulator(payload):\n    return {'args': [], 'env': {}}\n",
        encoding="utf-8",
    )
    (package / "plugin.json").write_text(json.dumps({
        "id": plugin_id,
        "name": "Vendor Config",
        "version": "1.0.0",
        "api_version": 1,
        "entry": "plugin.py",
        "hooks": ["emulator"],
        "permissions": [],
        "capabilities": {
            "emulator_settings": [{
                "emulator_id": emulator_id,
                "label": "Vendor MAME",
                "schema": {
                    "type": "object",
                    "properties": {"cheat_mode": {"type": "boolean", "default": False}},
                },
                "scopes": ["global"],
            }],
        },
    }), encoding="utf-8")
    return Path(root)


def test_sections_and_apply():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        state = {"emulator_settings": {
            plugin_section_key("vendor.cfg", "mame"): {
                "managed": True,
                "global": {"cheat_mode": True},
            },
        }}
        sections = plugin_emulator_sections(state, root)
        assert len(sections) == 1
        assert sections[0]["label"] == "Vendor MAME"

        with mock.patch("plugins.run_plugin_hook", return_value=(
            {"args": ["-nowindow"], "env": {"MAME_EXTRA": "1"}}, ""
        )) as hook:
            argv, env = apply_plugin_settings(
                "mame", "Arcade", ["mame", "game.zip"], state, Path(directory), []
            )
        assert argv == ["mame", "-nowindow", "game.zip"]
        assert env == {"MAME_EXTRA": "1"}
        payload = hook.call_args.args[3]
        assert payload["action"] == "apply"
        assert payload["values"]["cheat_mode"] is True

        with mock.patch("plugins.run_plugin_hook", return_value=(
            {"args": [], "env": {"LD_PRELOAD": "bad.so"}}, ""
        )):
            warnings = []
            argv, env = apply_plugin_settings(
                "mame", "Arcade", ["mame", "game.zip"], state, Path(directory), warnings
            )
        assert argv == ["mame", "game.zip"]
        assert env == {}
        assert warnings and "LD_PRELOAD" in warnings[0]


def test_unmanaged_section_is_not_applied():
    with TemporaryDirectory() as directory:
        _make_plugin(Path(directory) / "plugins")
        state = {"emulator_settings": {
            plugin_section_key("vendor.cfg", "mame"): {"managed": False, "global": {}},
        }}
        with mock.patch("plugins.run_plugin_hook") as hook:
            argv, env = apply_plugin_settings("mame", "Arcade", ["mame", "game.zip"], state, Path(directory), [])
        assert argv == ["mame", "game.zip"]
        assert env == {}
        hook.assert_not_called()


if __name__ == "__main__":
    test_sections_and_apply()
    test_unmanaged_section_is_not_applied()
    print("plugin emulator self-test: ok")
