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
        # A manifest without any version is refused loudly at read time; the
        # frozen API surfaces it (valid: False) rather than hiding it, and the
        # runner skips it.
        _make_plugin(root, "noversion.plugin", "def before_launch(p):\n    return p\n", version="")
        from plugins import read_manifest
        try:
            read_manifest(root / "noversion.plugin")
            raise AssertionError("a manifest without a version must be refused")
        except ValueError:
            pass
        entry = next(item for item in list_plugins(root) if item["id"] == "noversion.plugin")
        assert entry["valid"] is False and entry["error"]
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}, clear=True):
            result = run_plugins(root, "before_launch", {"args": []})
        assert "noversion.plugin" not in result["args"]
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


def test_plugin_api_v1():
    """Frozen API v1 surface — commands, invalid surfacing."""
    import json as _json
    import tempfile as _tempfile
    from pathlib import Path as _Path
    import plugins as _plugins

    with _tempfile.TemporaryDirectory() as directory:
        root = _Path(directory)
        package = root / "command.plugin"
        package.mkdir()
        (package / "plugin.json").write_text(_json.dumps({
            "id": "command.plugin", "name": "Command", "version": "1.0.0",
            "api_version": 1, "hooks": ["command"],
            "commands": [{"id": "do-it", "label": "Do it", "description": "test"}],
        }))
        (package / "plugin.py").write_text(
            "def command(payload):\n"
            "    return {'notification': {'level': 'success', 'message': 'did ' + payload['command']}}\n"
        )
        broken = root / "broken.plugin"
        broken.mkdir()
        (broken / "plugin.json").write_text("{ not json")
        listed = _plugins.list_plugins(root)
        by_id = {item["id"]: item for item in listed}
        assert by_id["command.plugin"]["valid"] is True
        assert by_id["command.plugin"]["api_version"] == 1
        assert by_id["broken.plugin"]["valid"] is False
        assert by_id["broken.plugin"]["error"]
        assert by_id["broken.plugin"]["sandbox"] in {"ready", "unavailable", "disabled"}

        commands = _plugins.plugin_commands(root)
        assert [item["id"] for item in commands] == ["do-it"]
        assert commands[0]["plugin_id"] == "command.plugin"
        _plugins.set_plugin_enabled(root, "command.plugin", False)
        assert _plugins.plugin_commands(root) == []
        assert len(_plugins.plugin_commands(root, include_disabled=True)) == 1
        _plugins.set_plugin_enabled(root, "command.plugin", True)

        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            result, error = _plugins.run_plugin_hook(root, "command.plugin", "command", {"command": "do-it", "library": []})
        assert error == "", error
        assert result["notification"]["message"] == "did do-it"
        result, error = _plugins.run_plugin_hook(root, "command.plugin", "before_launch", {})
        assert result is None and "hook" in error
        result, error = _plugins.run_plugin_hook(root, "broken.plugin", "command", {})
        assert result is None and error

        # Unavailable sandbox refuses to run instead of silently falling back.
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("plugins._sandbox_available", return_value=False):
            assert _plugins.sandbox_status() == "unavailable"
            result, error = _plugins.run_plugin_hook(root, "command.plugin", "command", {"command": "do-it"})
        assert result is None and "bubblewrap" in error

        # A manifest that requires a newer API version is surfaced, not hidden.
        newer = root / "newer.plugin"
        newer.mkdir()
        (newer / "plugin.json").write_text(_json.dumps({
            "id": "newer.plugin", "name": "Newer", "version": "1",
            "api_version": 99, "hooks": ["command"],
        }))
        (newer / "plugin.py").write_text("def command(payload):\n    return payload\n")
        entry = next(item for item in _plugins.list_plugins(root) if item["id"] == "newer.plugin")
        assert entry["valid"] is False and "API v99" in entry["error"]
    print("  plugin api v1: ok")


def test_plugin_routes():
    """The palette command routes run plugins through the sandbox."""
    import json as _json
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from types import SimpleNamespace

    from handlers.extensions import ExtensionsHandlers

    class Dummy(ExtensionsHandlers):
        def __init__(self):
            self.responses = []

        def send_json(self, status, payload, **kwargs):
            self.responses.append((status, payload))

    with _tempfile.TemporaryDirectory() as directory:
        root = _Path(directory)
        package = root / "plugins" / "command.plugin"
        package.mkdir(parents=True)
        (package / "plugin.json").write_text(_json.dumps({
            "id": "command.plugin", "name": "Command", "version": "1.0.0",
            "api_version": 1, "hooks": ["command"],
            "commands": [{"id": "do-it", "label": "Do it"}],
        }))
        (package / "plugin.py").write_text(
            "def command(payload):\n"
            "    return {'notification': {'level': 'success', 'message': 'did ' + payload['command'] + ' for ' + str(len(payload['library']))}}\n"
        )
        with mock.patch("handlers.extensions.DATA", root / "library.json"), mock.patch(
            "handlers.extensions.load_state_view",
            return_value={"games": [{"game_id": "g1", "name": "Game"}]},
        ), mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            commands = Dummy()
            commands._api_get_api_v2_plugins_commands(SimpleNamespace(query=""))
            status, payload = commands.responses[-1]
            assert status == 200 and payload["api_version"] == 1
            assert payload["commands"][0]["id"] == "do-it"
            assert payload["commands"][0]["plugin_id"] == "command.plugin"

            listed = Dummy()
            listed._api_get_api_plugins(SimpleNamespace(query=""))
            assert listed.responses[-1][1]["sandbox"] == "disabled"

            run = Dummy()
            run._api_post_api_v2_plugins_command({"plugin_id": "command.plugin", "command": "do-it"})
            assert run.responses[-1][1]["notification"]["message"] == "did do-it for 1"

            with mock.patch("handlers.extensions.read_manifest", side_effect=ValueError), pytest_raises(ValueError):
                run._api_post_api_v2_plugins_command({"plugin_id": "command.plugin", "command": "do-it"})
            try:
                run._api_post_api_v2_plugins_command({"plugin_id": "command.plugin", "command": "unknown"})
                raise AssertionError("unknown command accepted")
            except ValueError as error:
                assert "declare" in str(error)
    print("  plugin routes: ok")


class pytest_raises:
    """Tiny context manager so this stdlib-only suite avoids pytest imports."""

    def __init__(self, exception):
        self.exception = exception

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"expected {self.exception.__name__}")
        return issubclass(exc_type, self.exception)


def main():
    test()
    test_sandbox_permission_denial_surfaces()
    test_sandbox_plugin_error_carries_plugin_id()
    test_sandbox_slow_plugin_warns()
    test_sandbox_disabled_plugin_spawns_nothing()
    test_sandbox_manifest_versions()
    test_sandbox_process_group_isolation()
    test_sandbox_process_group_cleanup()
    test_plugin_api_v1()
    test_plugin_routes()
    test_plugin_trust_flow()
    test_plugin_trust_gates_unsandboxed_execution()
    test_plugin_share_net_argv()
    test_plugin_permissions()
    test_plugin_settings_validation()
    test_plugin_settings_roundtrip_and_stdin()
    test_plugin_remove_clears_trust_permissions_settings()
    test_plugin_library_source_merge()
    test_plugin_lifecycle_events()
    test_plugin_v2_routes()
    print("plugin sandbox self-test: ok")


# ---------------------------------------------------------------------------
# Plugins 2.0 (F1a/F1b/F1c/F1d/F1e/F1f) coverage.
# ---------------------------------------------------------------------------

def _make_20_plugin(root, plugin_id, code, manifest_extra=None):
    package = Path(root) / plugin_id
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id, "name": plugin_id, "version": "1.0.0",
        "api_version": 1, "hooks": ["events"],
    }
    manifest.update(manifest_extra or {})
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "plugin.py").write_text(code, encoding="utf-8")
    return package


def test_plugin_trust_flow():
    """F1a: trust grants are per-plugin, checksum-bound, and revocable."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "trust.plugin", "def before_launch(p):\n    return p\n")
        status = _plugins.plugin_trust_status(root, "trust.plugin")
        assert status["trusted"] is False and status["checksum_matches"] is False
        assert status["checksum"], "checksum is reported even before trust"
        # Deny-by-default: nothing trusted without an explicit grant.
        assert _plugins.set_plugin_trust(root, "trust.plugin", True) is True
        status = _plugins.plugin_trust_status(root, "trust.plugin")
        assert status["trusted"] is True and status["checksum_matches"] is True
        # A package update changes the checksum and re-prompts.
        (root / "trust.plugin" / "plugin.py").write_text("def before_launch(p):\n    return {}\n")
        status = _plugins.plugin_trust_status(root, "trust.plugin")
        assert status["trusted"] is False and status["checksum_matches"] is False
        # Re-grant on the new package, then revoke.
        _plugins.set_plugin_trust(root, "trust.plugin", True)
        assert _plugins.plugin_trust_status(root, "trust.plugin")["trusted"] is True
        _plugins.set_plugin_trust(root, "trust.plugin", False)
        status = _plugins.plugin_trust_status(root, "trust.plugin")
        assert status["trusted"] is False
    print("  plugin trust flow: ok")


def test_plugin_trust_gates_unsandboxed_execution():
    """F1a: untrusted plugins stay inert on sandbox-unavailable hosts."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_plugin(root, "gate.plugin", "def before_launch(p):\n    p['ran'] = True\n    return p\n")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("plugins._sandbox_available", return_value=False):
            assert _plugins.run_plugins(root, "before_launch", {"args": []}) == {"args": []}
        _plugins.set_plugin_trust(root, "gate.plugin", True)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("plugins._sandbox_available", return_value=False):
            assert _plugins.run_plugins(root, "before_launch", {"args": []}) == {"args": [], "ran": True}
        # Revoking restores the inert state.
        _plugins.set_plugin_trust(root, "gate.plugin", False)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("plugins._sandbox_available", return_value=False):
            assert _plugins.run_plugins(root, "before_launch", {"args": []}) == {"args": []}
    print("  plugin trust gates unsandboxed execution: ok")


def test_plugin_share_net_argv():
    """F1b: --share-net sits directly after --unshare-all, only when granted."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = _make_plugin(root, "net.plugin", "x = 1\n", {"hooks": []})
        entry = package / "plugin.py"
        with mock.patch("plugins.shutil.which", return_value="/usr/bin/bwrap"):
            granted = _plugins._sandboxed_command(package, entry, "before_launch", share_net=True)
            assert granted[granted.index("--unshare-all") + 1] == "--share-net"
            denied = _plugins._sandboxed_command(package, entry, "before_launch", share_net=False)
            assert "--share-net" not in denied
            # No bubblewrap on the host means no command at all.
        with mock.patch("plugins.shutil.which", return_value=None):
            assert _plugins._sandboxed_command(package, entry, "before_launch", share_net=True) is None
    print("  plugin --share-net argv: ok")


def test_plugin_permissions():
    """F1b: unknown permissions are rejected; grants default to deny."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_20_plugin(root, "perm.plugin", "x = 1\n", {"hooks": [], "permissions": ["network"]})
        assert _plugins.plugin_permission_grants(root, "perm.plugin") == []
        with pytest_raises(ValueError):
            _plugins.set_plugin_permissions(root, "perm.plugin", ["network", "everything"])
        # Rejected grants leave the previous (empty) state untouched.
        assert _plugins.plugin_permission_grants(root, "perm.plugin") == []
        _plugins.set_plugin_permissions(root, "perm.plugin", ["network"])
        assert _plugins.plugin_permission_grants(root, "perm.plugin") == ["network"]
        # Removal clears the grant: a reinstall must re-prompt.
        _plugins.remove_plugin(root, "perm.plugin")
        with TemporaryDirectory() as other:
            _make_20_plugin(Path(other), "perm.plugin", "x = 1\n", {"hooks": []})
            assert _plugins.plugin_permission_grants(Path(other), "perm.plugin") == []
    print("  plugin permissions: ok")


def test_plugin_settings_validation():
    """F1c: schema validation for types, enums, ranges, required, defaults."""
    import plugins as _plugins
    schema = {
        "type": "object",
        "properties": {
            "api_key": {"type": "string", "format": "password", "title": "API key"},
            "region": {"type": "string", "enum": ["us", "eu"], "default": "us"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
            "verbose": {"type": "boolean", "default": False},
        },
        "required": ["api_key"],
    }
    cleaned = _plugins.validate_plugin_settings(schema, {"api_key": "sekret"})
    assert cleaned == {"api_key": "sekret", "region": "us", "timeout": 10, "verbose": False}
    for bad in (
        {},  # missing required api_key
        {"api_key": "x", "region": "mars"},  # bad enum
        {"api_key": "x", "timeout": 0},  # below minimum
        {"api_key": "x", "timeout": 61},  # above maximum
        {"api_key": "x", "timeout": 1.5},  # non-integer for integer
        {"api_key": "x", "verbose": "yes"},  # non-boolean
        {"api_key": "x", "unknown_key": 1},  # unknown keys rejected
    ):
        with pytest_raises(ValueError):
            _plugins.validate_plugin_settings(schema, bad)
    print("  plugin settings validation: ok")


def test_plugin_settings_roundtrip_and_stdin():
    """F1c: stored settings validate, persist, and reach hooks via stdin."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        schema = {
            "type": "object",
            "properties": {"timeout": {"type": "integer", "minimum": 1, "default": 5}},
        }
        _make_20_plugin(
            root, "settings.plugin",
            "def events(payload):\n    return {'saw': payload['settings']['timeout']}\n",
            {"settings": schema},
        )
        stored = _plugins.set_plugin_settings(root, "settings.plugin", {"timeout": 30})
        assert stored == {"timeout": 30}
        form = _plugins.get_plugin_settings(root, "settings.plugin")
        assert form["values"] == {"timeout": 30}
        assert form["schema"]["properties"]["timeout"]["default"] == 5
        # Settings travel in the stdin JSON payload.
        _plugins.set_plugin_trust(root, "settings.plugin", True)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("plugins._sandbox_available", return_value=False):
            result, error = _plugins.run_plugin_hook(root, "settings.plugin", "events", {"event": "app_startup"})
        assert error == "", error
        assert result["saw"] == 30
        # A schema update that invalidates stored values falls back to defaults.
        _plugins.set_plugin_settings(root, "settings.plugin", {"timeout": 30})
        raw = _plugins.load_plugin_state(root)
        raw["settings"]["settings.plugin"]["timeout"] = "not-an-int"
        _plugins.save_plugin_state(root, raw)
        manifest = _plugins.read_manifest(root / "settings.plugin")
        assert _plugins.plugin_settings_values(root, manifest) == {"timeout": 5}
    print("  plugin settings roundtrip and stdin: ok")


def test_plugin_remove_clears_trust_permissions_settings():
    """F1a/F1b/F1c: removing a plugin drops all of its approval state."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_20_plugin(
            root, "stateful.plugin", "def events(p):\n    return p\n",
            {"permissions": ["network"],
             "settings": {"type": "object", "properties": {"x": {"type": "integer", "default": 1}}}},
        )
        _plugins.set_plugin_trust(root, "stateful.plugin", True)
        _plugins.set_plugin_permissions(root, "stateful.plugin", ["network"])
        _plugins.set_plugin_settings(root, "stateful.plugin", {"x": 2})
        assert _plugins.plugin_trust_status(root, "stateful.plugin")["trusted"] is True
        _plugins.remove_plugin(root, "stateful.plugin")
        state = _plugins.load_plugin_state(root)
        assert "stateful.plugin" not in state.get("trust", {})
        assert "stateful.plugin" not in state.get("permissions", {})
        assert "stateful.plugin" not in state.get("settings", {})
    print("  plugin remove clears trust/permissions/settings: ok")


def test_plugin_library_source_merge():
    """F1d: namespaced ids, provenance, dedupe, disable-drop, validation."""
    import plugins as _plugins
    with TemporaryDirectory() as directory:
        root = Path(directory)
        data_parent = root / "data"
        plugins_dir = data_parent / "plugins"
        plugins_dir.mkdir(parents=True)
        _make_20_plugin(
            plugins_dir, "source.plugin",
            "def library_source(p):\n    return {'games': []}\n",
            {"hooks": ["library_source"], "name": "Source Plugin"},
        )
        base = [{"game_id": "g1", "name": "Base Game"}]

        def fake_run_hook(directory, plugin_id, hook, payload):
            assert hook == "library_source"
            return ({"games": [
                {"id": "ext-1", "name": "External One", "platform": "PC"},
                {"id": "ext-1", "name": "External One Duplicate"},
                {"id": "ext-2", "name": "External Two"},
                "not-a-dict",
                {"name": "Nameless Entry"},
            ]}, "")

        with mock.patch("plugins.run_plugin_hook", side_effect=fake_run_hook):
            merged = _plugins.merge_library_source_games(list(base), data_parent)
        assert len(merged) == 1 + 3, [game["name"] for game in merged]
        imported = merged[1:]
        assert imported[0]["game_id"] == "plugin:source.plugin:ext-1"
        assert imported[0]["plugin_source"] == "source.plugin"
        assert imported[0]["plugin_source_name"] == "Source Plugin"
        assert all(game["id"] == 1 + offset for offset, game in enumerate(imported))
        # Disabling the plugin drops its games on the next merge.
        _plugins.set_plugin_enabled(plugins_dir, "source.plugin", False)
        with mock.patch("plugins.run_plugin_hook", side_effect=fake_run_hook):
            merged = _plugins.merge_library_source_games(list(base), data_parent)
        assert merged == base
    print("  plugin library_source merge: ok")


def test_plugin_lifecycle_events():
    """F1e: unified events hook payloads and centralized game diffs."""
    import plugins as _plugins
    calls = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _make_20_plugin(root, "events.plugin", "def events(p):\n    return p\n")
        with mock.patch("plugins.run_plugins", side_effect=lambda directory, hook, payload: calls.append((hook, payload)) or payload):
            _plugins.emit_plugin_event(root, "app_startup", {})
            _plugins.emit_plugin_event(root, "scan_finished", {"folder": "/games", "added": 3, "scanned": 40})
            _plugins.emit_plugin_event(root, "playtime_milestone", {"game_id": "g1", "hours": 2})
        assert calls[0] == ("events", {"event": "app_startup"})
        assert calls[1][1]["event"] == "scan_finished" and calls[1][1]["added"] == 3
        assert calls[2][1]["event"] == "playtime_milestone" and calls[2][1]["hours"] == 2
        # One hook name for every event (ADR 0057), never per-event hooks.
        assert all(hook == "events" for hook, _ in calls)
        # Safe mode suppresses all emission.
        calls.clear()
        with mock.patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
            _plugins.emit_plugin_event(root, "app_startup", {})
        assert calls == []
        # Game diffs: add/remove/update with bounded payloads.
        calls.clear()
        before = _plugins.snapshot_library({"games": [
            {"game_id": "g1", "name": "Same"},
            {"game_id": "g2", "name": "Gone"},
            {"game_id": "g3", "name": "Changed", "favorite": False},
        ]})
        after = _plugins.snapshot_library({"games": [
            {"game_id": "g1", "name": "Same"},
            {"game_id": "g3", "name": "Changed", "favorite": True},
            {"game_id": "g4", "name": "New"},
        ]})
        with mock.patch("plugins.run_plugins", side_effect=lambda directory, hook, payload: calls.append(payload) or payload):
            _plugins.emit_library_diff(root, before, after)
        by_event = {payload["event"]: payload for payload in calls}
        assert by_event["game_added"]["game_ids"] == ["g4"]
        assert by_event["game_removed"]["game_ids"] == ["g2"]
        assert by_event["game_updated"]["changes"] == [{"game_id": "g3", "changed": ["favorite"]}]
        # Payloads carry ids only, never full game objects.
        for payload in calls:
            assert "games" not in payload
    print("  plugin lifecycle events: ok")


def test_plugin_v2_routes():
    """F1a/F1b/F1c/F1f: trust, permissions, settings, and enriched catalog."""
    import plugins as _plugins
    from types import SimpleNamespace
    from handlers.extensions import ExtensionsHandlers
    from plugin_catalog import load_local_catalog

    class Dummy(ExtensionsHandlers):
        def __init__(self):
            self.responses = []

        def send_json(self, status, payload, **kwargs):
            self.responses.append((status, payload))

    with TemporaryDirectory() as directory:
        root = Path(directory)
        plugins_dir = root / "plugins"
        plugins_dir.mkdir()
        schema = {"type": "object", "properties": {"level": {"type": "integer", "default": 1}}}
        _make_20_plugin(
            plugins_dir, "route.plugin", "def events(p):\n    return p\n",
            {"permissions": ["network"], "settings": schema},
        )
        with mock.patch("handlers.extensions.DATA", root / "library.json"):
            trust = Dummy()
            trust._api_get_api_v2_plugins_trust(SimpleNamespace(query="id=route.plugin"))
            assert trust.responses[-1][0] == 200
            assert trust.responses[-1][1]["trusted"] is False
            trust._api_post_api_v2_plugins_trust({"id": "route.plugin", "trusted": True})
            assert trust.responses[-1][1]["trusted"] is True
            assert trust.responses[-1][1]["id"] == "route.plugin"
            trust._api_get_api_v2_plugins_trust(SimpleNamespace(query="id=route.plugin"))
            assert trust.responses[-1][1]["trusted"] is True
            with pytest_raises(ValueError):
                trust._api_post_api_v2_plugins_trust({"id": "route.plugin"})  # missing trusted flag
            with pytest_raises(ValueError):
                trust._api_post_api_v2_plugins_trust({"id": "nope.plugin", "trusted": True})

            perms = Dummy()
            with pytest_raises(ValueError):
                perms._api_post_api_v2_plugins_permissions({"id": "route.plugin", "permissions": ["root"]})
            perms._api_post_api_v2_plugins_permissions({"id": "route.plugin", "permissions": ["network"]})
            assert perms.responses[-1][1]["permissions"] == ["network"]
            assert perms.responses[-1][1]["id"] == "route.plugin"
            listed = _plugins.list_plugins(plugins_dir)
            assert listed[0]["granted_permissions"] == ["network"]

            settings = Dummy()
            settings._api_get_api_v2_plugins_settings(SimpleNamespace(query="id=route.plugin"))
            assert settings.responses[-1][1]["values"] == {"level": 1}
            settings._api_post_api_v2_plugins_settings({"id": "route.plugin", "values": {"level": 3}})
            assert settings.responses[-1][1]["values"] == {"level": 3}
            with pytest_raises(ValueError):
                settings._api_post_api_v2_plugins_settings({"id": "route.plugin", "values": {"level": "high"}})

            catalog = Dummy()
            local_entries = load_local_catalog()
            assert local_entries, "expected bundled local catalog entries"
            with mock.patch("handlers.extensions.fetch_plugin_catalog", return_value=local_entries):
                catalog._api_get_api_v2_plugins_catalog(SimpleNamespace(query=""))
            status, payload = catalog.responses[-1]
            assert status == 200
            assert isinstance(payload["catalog"], list)
            assert all("installed" in entry and "update_available" in entry for entry in payload["catalog"])
    print("  plugin v2 routes: ok")


if __name__ == "__main__":
    main()
