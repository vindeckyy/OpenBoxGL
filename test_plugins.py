#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from plugin_catalog import download_plugin_package, load_local_catalog, REMOTE_CATALOG
from plugins import install_plugin, list_plugins, remove_plugin, run_plugins, set_plugin_enabled


def test():
    assert "/master/" not in REMOTE_CATALOG
    assert len(REMOTE_CATALOG.split("/")[-3]) == 40
    assert any(entry.get("id") == "openbox.library-stats" and entry.get("local_only") for entry in load_local_catalog())
    # A remote catalog entry without a valid sha256 must be refused before any download.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        for bad_entry in (
            {"id":"no-checksum", "url":"https://example.invalid/plugin.zip"},
            {"id":"bad-checksum", "url":"https://example.invalid/plugin.zip", "sha256":"deadbeef"},
            {"id":"insecure", "url":"http://example.invalid/plugin.zip", "sha256":"0" * 64},
            {"id":"unsafe/../path", "url":"https://example.invalid/plugin.zip", "sha256":"0" * 64},
        ):
            try:
                download_plugin_package(bad_entry, directory)
                raise AssertionError("expected ValueError for missing sha256")
            except ValueError:
                pass
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "source"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({
            "id":"test.plugin", "name":"Test", "version":"1", "hooks":["before_launch"],
            "sha256": hashlib.sha256(b"test.plugin v1").hexdigest(),
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
            "sha256": hashlib.sha256(b"test.plugin v2").hexdigest(),
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

        # A before_launch plugin must not be able to swap the binary or point the
        # working directory outside the game/data directories: start_game falls
        # back to the original launch command instead of running the tampered one.
        env_backup = dict(os.environ)
        try:
            import tempfile
            import unittest.mock as mock
            with tempfile.TemporaryDirectory() as data_dir:
                os.environ["OPENBOX_DATA_DIR"] = data_dir
                os.environ.pop("OPENBOX_SAFE_MODE", None)
                from openbox import save_state
                import webapp_state
                from pathlib import Path as _Path
                game_dir = _Path(data_dir) / "games"
                game_dir.mkdir(parents=True)
                game_file = game_dir / "game.sh"
                game_file.write_text("#!/bin/sh\n")
                save_state({"games": [{"name": "Escape", "path": str(game_file)}], "profiles": {}, "history": []})

                def tamper(_directory, _hook, payload):
                    payload["args"] = ["/bin/sh", "-c", "echo pwned > /tmp/plugin-escape"]
                    payload["cwd"] = "/"
                    return payload

                process = type("Process", (), {"pid": 4242, "wait": lambda self: 0, "poll": lambda self: 0})()
                with mock.patch("webapp_state.subprocess.Popen", return_value=process) as popen:
                    with mock.patch("webapp_state.run_plugins", side_effect=tamper) as hook:
                        with mock.patch("webapp_state.finish_session"):
                            webapp_state.start_game(0)
                hook.assert_called_once()
                launched = popen.call_args[0][0]
                assert launched == ["bash", str(game_file)], launched
                # Original cwd is the game directory; a valid plugin result is kept.
                with mock.patch("webapp_state.subprocess.Popen", return_value=process) as popen:
                    with mock.patch("webapp_state.run_plugins", side_effect=lambda _directory, _hook, payload: {
                        "args": payload["args"] + ["--ok"], "cwd": payload["cwd"],
                    }):
                        with mock.patch("webapp_state.finish_session"):
                            webapp_state.start_game(0)
                assert popen.call_args[0][0][-1] == "--ok"
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    # The plugin environment filter must strip correctly spelled Gameyfin
    # variables: the pattern is GAMEYFIN_, not GAMEFYIN_. A real plugin run
    # proves the filtered environment, not a copy of the filter code.
    import json as _json
    import tempfile as _tempfile
    import unittest.mock as _mock
    from pathlib import Path as _Path
    import plugins as _plugins
    with _tempfile.TemporaryDirectory() as directory:
        root = _Path(directory)
        plugin = root / "env.dump"
        (plugin / "plugin.py").parent.mkdir(parents=True, exist_ok=True)
        (plugin / "plugin.py").write_text(
            "import json, os\n"
            "def before_launch(payload):\n"
            "    open(os.environ['ENV_DUMP'], 'w').write(json.dumps("
            "{k: v for k, v in os.environ.items() if 'GAMEFYIN' in k or 'GAMEYFIN' in k}))\n"
            "    return payload\n"
        )
        (plugin / "plugin.json").write_text(_json.dumps({
            "id": "env.dump", "name": "env dump", "version": "1",
            "entry": "plugin.py", "hooks": ["before_launch"],
        }))
        dump = root / "env.json"
        env = dict(os.environ)
        env["GAMEYFIN_URL"] = "http://internal"
        env["GAMEYFIN_PASSWORD"] = "secret"
        env["ENV_DUMP"] = str(dump)
        with _mock.patch.dict(os.environ, env, clear=True):
            _plugins.run_plugins(root, "before_launch", {"args": []})
        leaked = _json.loads(dump.read_text())
        assert leaked == {}, leaked
    print("plugin self-test: ok")


if __name__ == "__main__":
    test()
