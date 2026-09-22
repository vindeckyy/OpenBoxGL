#!/usr/bin/env python3
"""Plugin packaging/hook tests plus the sandbox confinement regression suite.

Known limitation (documented, not fixed here): with start_new_session=True,
only the direct plugin child is SIGKILLed on timeout — a double-forking
plugin persists as an orphan with full user privileges when unsandboxed.
Process-group kill is a 1.14 hardening item.
"""
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugin_catalog import download_plugin_package, load_local_catalog, REMOTE_CATALOG  # noqa: E402
from plugins import install_plugin, list_plugins, remove_plugin, run_plugins, set_plugin_enabled  # noqa: E402


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
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("plugins._sandbox_available", return_value=False):
                assert run_plugins(plugins, "before_launch", {"args":["game"]})["args"] == ["game"]
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
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
        (broken / "plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
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
            with tempfile.TemporaryDirectory() as data_dir:
                os.environ["OPENBOX_DATA_DIR"] = data_dir
                os.environ.pop("OPENBOX_SAFE_MODE", None)
                from openbox import save_state
                import webapp_state
                from pathlib import Path as _Path
                game_dir = _Path(data_dir) / "games"
                game_dir.mkdir(parents=True)
                game_file = game_dir / ("game.sh" if os.name != "nt" else "game.exe")
                game_file.write_text("#!/bin/sh\n" if os.name != "nt" else "", encoding="utf-8")
                save_state({"games": [{"name": "Escape", "path": str(game_file)}], "profiles": {}, "history": []})

                def tamper(_directory, _hook, payload):
                    payload["args"] = (["/bin/sh", "-c", "echo pwned > /tmp/plugin-escape"]
                                       if os.name != "nt"
                                       else [str(game_file), "--pwned"])
                    payload["cwd"] = "/"
                    return payload

                process = type("Process", (), {"pid": 4242, "wait": lambda self: 0, "poll": lambda self: 0})()
                with mock.patch("webapp_state.subprocess.Popen", return_value=process) as popen:
                    with mock.patch("webapp_state.run_plugins", side_effect=tamper) as hook:
                        with mock.patch("webapp_state.finish_session"):
                            webapp_state.start_game(0)
                hook.assert_called_once()
                launched = popen.call_args[0][0]
                if os.name != "nt":
                    assert launched == ["bash", str(game_file)], launched
                else:
                    assert launched == [str(game_file)], launched
                # The production session watcher owns cleanup.  This test
                # mocks finish_session, so release its registries explicitly
                # before exercising a second launch.
                from pkg.state.registry import PENDING_LAUNCHES, PROCESSES, RUNNING
                RUNNING.clear()
                PROCESSES.clear()
                PENDING_LAUNCHES.clear()
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
        }), encoding="utf-8")
        dump = root / "env.json"
        env = dict(os.environ)
        env["GAMEYFIN_URL"] = "http://internal"
        env["GAMEYFIN_PASSWORD"] = "secret"
        env["ENV_DUMP"] = str(dump)
        with _mock.patch.dict(os.environ, {**env, "OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
            _plugins.run_plugins(root, "before_launch", {"args": []})
        leaked = _json.loads(dump.read_text(encoding="utf-8"))
        assert leaked == {}, leaked
    print("plugin self-test: ok")


# ---------------------------------------------------------------------------
# Sandbox confinement regression suite (§6.1). A regression dropping a
# containment mechanism (the mount profile, the timeout, the payload caps,
# the output contract, the env filter, start_new_session) must be visible
# here. Plugin failures surface with the plugin id in the server log — the
# equivalent of the UI error toast carrying plugin_id — never silently.
# ---------------------------------------------------------------------------

def _make_plugin(root, plugin_id, code, hooks=("before_launch",), version="1"):
    package = Path(root) / plugin_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "plugin.json").write_text(json.dumps({
        "id": plugin_id, "name": plugin_id, "version": version,
        "hooks": list(hooks),
    }), encoding="utf-8")
    (package / "plugin.py").write_text(code, encoding="utf-8")
    return package


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_plugin_logs():
    logger = logging.getLogger("openbox.plugins")
    handler = _LogCapture()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    return logger, handler, old_level


def _plugin_warnings(handler):
    return [record.getMessage() for record in handler.records
            if record.levelno >= logging.WARNING]


def test_sandbox_permission_denial_surfaces():
    """Denying a plugin must surface (with its id), never fail silently.

    The plugin's declared hooks are its capabilities: when the sandbox is
    unavailable and the unsandboxed escape hatch is not exactly "1", the
    plugin is skipped *and* the denial is logged naming the plugin.
    """
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "denied.plugin",
                     "def before_launch(p):\n    p['ran'] = True\n    return p\n")
        logger, handler, old_level = _capture_plugin_logs()
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("plugins._sandbox_available", return_value=False):
                    result = run_plugins(root, "before_launch", {"args": []})
            assert result == {"args": []}, "denied plugin must not run"
            warnings = _plugin_warnings(handler)
            assert any("denied.plugin" in message and "bubblewrap" in message
                       for message in warnings), warnings
            # A non-"1" value does not open the unsandboxed path either: the
            # variable is the *only* path to unsandboxed execution.
            handler.records.clear()
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "0"}, clear=True):
                with mock.patch("plugins._sandbox_available", return_value=False):
                    result = run_plugins(root, "before_launch", {"args": []})
            assert result == {"args": []}
            warnings = _plugin_warnings(handler)
            assert any("denied.plugin" in message for message in warnings), warnings
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    print("  sandbox permission denial surfaces: ok")


def test_sandbox_plugin_error_carries_plugin_id():
    """Every plugin failure is reported with the plugin id (the error toast)."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "err.plugin",
                     "import sys\ndef before_launch(p):\n    sys.exit(3)\n")
        _make_plugin(root, "json.plugin",
                     "def before_launch(p):\n    print('not json{{')\n    return p\n")
        logger, handler, old_level = _capture_plugin_logs()
        try:
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
                result = run_plugins(root, "before_launch", {"args": []})
            assert result == {"args": []}, "failed plugins must not alter the payload"
            warnings = _plugin_warnings(handler)
            assert any("err.plugin" in message and "status 3" in message
                       for message in warnings), warnings
            assert any("json.plugin" in message and "invalid JSON" in message
                       for message in warnings), warnings
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    print("  sandbox plugin errors carry plugin_id: ok")


def test_sandbox_slow_plugin_warns():
    """A plugin slower than the 5 s timeout is killed and warned about.

    It must not block the hook chain: the run returns after ~5 s (not the
    plugin's 10 s sleep) with the payload unchanged and a warning naming
    the plugin.
    """
    import time
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "slow.plugin",
                     "import time\ndef before_launch(p):\n    time.sleep(10)\n    return p\n")
        logger, handler, old_level = _capture_plugin_logs()
        try:
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
                start = time.monotonic()
                result = run_plugins(root, "before_launch", {"args": []})
                elapsed = time.monotonic() - start
            assert result == {"args": []}
            assert 4.5 <= elapsed < 9.0, f"slow plugin must die at the 5 s timeout, took {elapsed:.1f}s"
            warnings = _plugin_warnings(handler)
            assert any("slow.plugin" in message and "before_launch" in message
                       for message in warnings), warnings
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    print("  sandbox slow plugin warns instead of blocking: ok")


def test_sandbox_disabled_plugin_spawns_nothing():
    """A disabled plugin leaves no timers/handlers: nothing is ever spawned."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        marker = root / "ran.marker"
        _make_plugin(root, "quiet.plugin",
                     f"def before_launch(p):\n    open({str(marker)!r}, 'w').write('ran')\n    return p\n")
        set_plugin_enabled(root, "quiet.plugin", False)
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
            with mock.patch("plugins.subprocess.run") as spawned:
                result = run_plugins(root, "before_launch", {"args": []})
        assert spawned.call_count == 0, "disabled plugin must not spawn a process"
        assert not marker.exists(), "disabled plugin must not run its hook"
        assert result == {"args": []}
    print("  sandbox disabled plugin spawns nothing: ok")


def test_sandbox_manifest_versions():
    """Manifests declaring version "1", "2", or semver all load and run."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for plugin_id, version in (("v1.plugin", "1"), ("v2.plugin", "2"), ("v3.plugin", "1.2.3")):
            _make_plugin(root, plugin_id,
                         f"def before_launch(p):\n    p['args'].append({plugin_id!r})\n    return p\n",
                         version=version)
        assert {entry["id"] for entry in list_plugins(root)} == {"v1.plugin", "v2.plugin", "v3.plugin"}
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
            result = run_plugins(root, "before_launch", {"args": []})
        assert sorted(result["args"]) == ["v1.plugin", "v2.plugin", "v3.plugin"]
        # A manifest without any version is refused loudly at read time and
        # skipped (not silently loaded) by the plugin listing.
        _make_plugin(root, "noversion.plugin", "def before_launch(p):\n    return p\n", version="")
        from plugins import read_manifest
        try:
            read_manifest(root / "noversion.plugin")
            raise AssertionError("a manifest without a version must be refused")
        except ValueError:
            pass
        assert "noversion.plugin" not in {entry["id"] for entry in list_plugins(root)}
    print("  sandbox manifest v1/v2 compat: ok")


def test_sandbox_process_group_isolation():
    """A misbehaving plugin cannot signal outside its own process group.

    The plugin kills process group 0 (itself). start_new_session=True puts
    the plugin in its own session/group, so the test process must survive —
    survival *is* the assertion. If the isolation regressed, this test dies
    loudly instead of passing silently.
    """
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "sig.plugin",
                     "import os, signal\ndef before_launch(p):\n"
                     "    os.kill(0, signal.SIGTERM)\n    return p\n")
        logger, handler, old_level = _capture_plugin_logs()
        try:
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
                result = run_plugins(root, "before_launch", {"args": []})
            # Still alive: the plugin's kill(0, SIGTERM) hit only its group.
            assert result == {"args": []}
            warnings = _plugin_warnings(handler)
            assert any("sig.plugin" in message for message in warnings), warnings
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
    print("  sandbox process group isolation: ok")


def test_sandbox_process_group_cleanup():
    """A timed-out plugin's process is killed and reaped — nothing lingers."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "linger.plugin",
                     "import os, time\ndef before_launch(p):\n"
                     "    open(p['pid_file'], 'w').write(str(os.getpid()))\n"
                     "    time.sleep(30)\n    return p\n")
        pid_file = Path(directory) / "plugin.pid"
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
            run_plugins(root, "before_launch", {"args": [], "pid_file": str(pid_file)})
        pid = int(pid_file.read_text(encoding="utf-8"))
        try:
            os.kill(pid, 0)
        except PermissionError:
            # Process exists but we cannot signal it: still alive.
            cleaned = False
        except OSError:
            # No such process. POSIX raises ProcessLookupError (an OSError
            # subclass) for a dead PID; Windows raises OSError [WinError 87]
            # ("The parameter is incorrect") instead.
            cleaned = True
        else:
            cleaned = False
        assert cleaned, "timed-out plugin left a live process behind"
        # And once disabled, it is never spawned again.
        set_plugin_enabled(root, "linger.plugin", False)
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
            with mock.patch("plugins.subprocess.run") as spawned:
                run_plugins(root, "before_launch", {"args": [], "pid_file": str(pid_file)})
        assert spawned.call_count == 0
    print("  sandbox process group cleanup: ok")


def main():
    test()
    test_sandbox_permission_denial_surfaces()
    test_sandbox_plugin_error_carries_plugin_id()
    test_sandbox_slow_plugin_warns()
    test_sandbox_disabled_plugin_spawns_nothing()
    test_sandbox_manifest_versions()
    test_sandbox_process_group_isolation()
    test_sandbox_process_group_cleanup()
    print("plugin sandbox self-test: ok")


if __name__ == "__main__":
    main()
