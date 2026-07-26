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
    print("plugin self-test: ok")


if __name__ == "__main__":
    test()
