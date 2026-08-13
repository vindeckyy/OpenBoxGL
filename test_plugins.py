#!/usr/bin/env python3
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from plugin_catalog import REMOTE_CATALOG
from plugins import install_plugin, list_plugins, remove_plugin, run_plugins, set_plugin_enabled


def test():
    assert REMOTE_CATALOG == "https://raw.githubusercontent.com/vindeckyy/OpenBoxGL/master/plugins/catalog.json"
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "source"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({
            "id":"test.plugin", "name":"Test", "version":"1", "hooks":["before_launch"],
        }))
        (package / "plugin.py").write_text(
            "def before_launch(payload):\n"
            "    payload['args'].append('--plugin-worked')\n"
            "    return payload\n"
        )
        plugins = root / "installed"
        assert not install_plugin(package, plugins)["updated"]
        result = run_plugins(plugins, "before_launch", {"args":["game"],"cwd":"/tmp"})
        assert result["args"][-1] == "--plugin-worked"
        set_plugin_enabled(plugins, "test.plugin", False)
        assert run_plugins(plugins, "before_launch", {"args":["game"]})["args"] == ["game"]
        assert not list_plugins(plugins)[0]["enabled"]
        assert remove_plugin(plugins, "test.plugin") == "test.plugin"
        assert list_plugins(plugins) == []

        # Reinstalling after removal comes back enabled.
        assert not install_plugin(package, plugins)["updated"]
        assert list_plugins(plugins)[0]["enabled"]

        # A failed update must restore the previous working version.
        broken = root / "broken"
        broken.mkdir()
        (broken / "plugin.json").write_text(json.dumps({
            "id":"test.plugin", "name":"Test", "version":"2", "hooks":["before_launch"],
        }))
        (broken / "plugin.py").write_text("raise RuntimeError('boom')\n")
        from unittest import mock
        with mock.patch("plugins.shutil.copytree", side_effect=OSError("disk full")):
            try:
                install_plugin(broken, plugins)
                raise AssertionError("expected OSError")
            except OSError:
                pass
        # The previous version is still installed and enabled.
        assert list_plugins(plugins)[0]["version"] == "1"
        assert list_plugins(plugins)[0]["enabled"]

        # A swap failure after the old copy moved to .backups must restore it.
        real_replace = Path.replace
        calls = {"count": 0}

        def failing_replace(self, target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("swap failed")
            return real_replace(self, target)

        with mock.patch.object(Path, "replace", failing_replace):
            try:
                install_plugin(broken, plugins)
                raise AssertionError("expected OSError")
            except OSError:
                pass
        # First replace: destination -> backup. Second: staging -> destination.
        assert calls["count"] >= 2
        assert list_plugins(plugins)[0]["version"] == "1"
        assert (plugins / "test.plugin" / "plugin.py").is_file()
        assert not (plugins / ".backups").exists() or not list((plugins / ".backups").iterdir())
    print("plugin self-test: ok")


if __name__ == "__main__":
    test()
