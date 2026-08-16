"""Local OpenBox plugin packages and hooks."""

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import os
from datetime import datetime
from pathlib import Path

from archives import safe_zip_extract
from backend_io import atomic_write_text


HOOKS = {"before_launch", "after_session", "library"}
PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
RUNNER = Path(__file__).with_name("plugin_runner.py")
LOGGER = logging.getLogger("openbox.plugins")
MAX_PLUGIN_PAYLOAD = 2 * 1024 * 1024
UNSANDBOXED_PLUGINS_ENV = "OPENBOX_ALLOW_UNSANDBOXED_PLUGINS"


def _sandboxed_command(package_root, entry, hook):
    """Build a bubblewrap command with no user-home or network access."""
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        return None
    relative_entry = entry.relative_to(package_root)
    return [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--ro-bind", "/", "/",
        "--tmpfs", "/home",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/mnt",
        "--tmpfs", "/media",
        "--proc", "/proc",
        "--dev", "/dev",
        "--dir", "/opt/openbox",
        "--dir", "/opt/openbox/plugin",
        "--dir", "/opt/openbox/runner",
        "--ro-bind", str(package_root), "/opt/openbox/plugin",
        "--ro-bind", str(RUNNER.parent), "/opt/openbox/runner",
        "--chdir", "/tmp",
        sys.executable,
        "-B",
        "/opt/openbox/runner/plugin_runner.py",
        f"/opt/openbox/plugin/{relative_entry}",
        hook,
    ]


def _sandbox_available():
    """Check whether this host can create the namespaces bubblewrap needs."""
    command = shutil.which("bwrap")
    if not command:
        return False
    probe = subprocess.run(
        [command, "--die-with-parent", "--new-session", "--unshare-all", "--ro-bind", "/", "/", "/usr/bin/true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )
    return probe.returncode == 0


def _plugin_command(package_root, entry, hook):
    if os.environ.get(UNSANDBOXED_PLUGINS_ENV) == "1":
        LOGGER.warning(
            "Running plugin %s without an OS sandbox because %s=1",
            package_root.name,
            UNSANDBOXED_PLUGINS_ENV,
        )
        return [sys.executable, "-B", str(RUNNER), str(entry), hook]
    command = _sandboxed_command(package_root, entry, hook)
    if command and _sandbox_available():
        return command
    LOGGER.warning(
        "Skipping plugin %s because bubblewrap is unavailable; set %s=1 only for trusted local plugins",
        package_root.name,
        UNSANDBOXED_PLUGINS_ENV,
    )
    return None


def state_file(directory):
    return Path(directory) / "plugins-state.json"


def load_plugin_state(directory):
    try:
        data = json.loads(state_file(directory).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_plugin_state(directory, state):
    path = state_file(directory)
    atomic_write_text(path, json.dumps(state, indent=2) + "\n")


def read_manifest(path):
    try:
        manifest = json.loads((path / "plugin.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Plugin package needs a valid plugin.json.") from error
    plugin_id = str(manifest.get("id", ""))
    entry = str(manifest.get("entry", "plugin.py"))
    hooks = manifest.get("hooks", [])
    if not PLUGIN_ID.fullmatch(plugin_id) or not manifest.get("name") or not manifest.get("version"):
        raise ValueError("Plugin id, name, and version are required.")
    if not isinstance(hooks, list) or not set(hooks) <= HOOKS:
        raise ValueError("Plugin declares an unsupported hook.")
    entry_path = (path / entry).resolve()
    if path.resolve() not in entry_path.parents or not entry_path.is_file() or entry_path.suffix != ".py":
        raise ValueError("Plugin entry must be a Python file inside the package.")
    return {**manifest, "id":plugin_id, "entry":entry, "hooks":hooks}


def list_plugins(directory):
    root = Path(directory)
    disabled = set(load_plugin_state(root).get("disabled", []))
    plugins = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            try:
                manifest = read_manifest(path)
            except ValueError:
                continue
            plugins.append({**manifest, "enabled":manifest["id"] not in disabled})
    return plugins


def install_plugin(source, directory):
    source, root = Path(source).expanduser(), Path(directory)
    if not source.exists() or (not source.is_dir() and source.suffix.casefold() != ".zip"):
        raise ValueError("Plugin path must be a directory or ZIP package.")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        staging = Path(temporary) / "package"
        if source.is_dir():
            if any(path.is_symlink() for path in source.rglob("*")):
                raise ValueError("Plugin directories may not contain symlinks.")
            shutil.copytree(source, staging)
        else:
            staging.mkdir()
            safe_zip_extract(source, staging)
        package = staging if (staging / "plugin.json").is_file() else next(
            (path for path in staging.iterdir() if path.is_dir() and (path / "plugin.json").is_file()),
            staging,
        )
        manifest = read_manifest(package)
        destination = root / manifest["id"]
        updated = destination.exists()
        backup = None
        if updated:
            backup = root / ".backups" / f"{manifest['id']}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
            backup.parent.mkdir(parents=True, exist_ok=True)
        staging = root / f".{manifest['id']}.installing"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(package, staging)
        try:
            if updated:
                destination.replace(backup)
            staging.replace(destination)
        except Exception:
            # A failed update must not leave the plugin uninstalled.
            if updated and backup and not destination.exists() and backup.exists():
                backup.replace(destination)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if backup and backup.exists() and destination.exists():
                shutil.rmtree(backup, ignore_errors=True)
    return {**manifest, "updated":updated}


def set_plugin_enabled(directory, plugin_id, enabled):
    root = Path(directory)
    manifest = read_manifest(root / plugin_id)
    state = load_plugin_state(root)
    disabled = set(state.get("disabled", []))
    disabled.discard(manifest["id"]) if enabled else disabled.add(manifest["id"])
    state["disabled"] = sorted(disabled)
    save_plugin_state(root, state)
    return enabled


def remove_plugin(directory, plugin_id):
    root = Path(directory)
    manifest = read_manifest(root / plugin_id)
    trash = root / ".removed" / f"{manifest['id']}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    (root / manifest["id"]).replace(trash)
    # Clear any disabled state so a reinstall comes back enabled.
    state = load_plugin_state(root)
    disabled = set(state.get("disabled", []))
    disabled.discard(manifest["id"])
    state["disabled"] = sorted(disabled)
    save_plugin_state(root, state)
    return manifest["id"]


def run_plugins(directory, hook, payload):
    result = payload
    for manifest in list_plugins(directory):
        if not manifest["enabled"] or hook not in manifest["hooks"]:
            continue
        package_root = Path(directory) / manifest["id"]
        entry = package_root / manifest["entry"]
        encoded = json.dumps(result)
        if len(encoded.encode("utf-8")) > MAX_PLUGIN_PAYLOAD:
            LOGGER.warning("Skipping plugin %s for %s because the payload is too large", manifest["id"], hook)
            continue
        command = _plugin_command(package_root, entry, hook)
        if command is None:
            continue
        try:
            plugin_env = os.environ.copy()
            for key in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"):
                plugin_env.pop(key, None)
            # Strip credentials and OpenBox-internal configuration so plugins
            # cannot read tokens, secrets, or host state out of the environment.
            sensitive = (
                "TOKEN", "PASSWORD", "SECRET", "API_KEY",
                "OPENBOX_", "RETROACHIEVEMENTS_", "EMUMOVIES_", "GITHUB_",
                "RA_", "IGDB_", "GAMEYFIN_",
            )
            upper_env = {key.upper(): key for key in plugin_env}
            for pattern in sensitive:
                for upper_key in list(upper_env):
                    if pattern in upper_key:
                        plugin_env.pop(upper_env.pop(upper_key), None)
            plugin_env["PYTHONNOUSERSITE"] = "1"
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                completed = subprocess.run(
                    command,
                    input=encoded.encode("utf-8"), stdout=stdout_file, stderr=stderr_file,
                    timeout=5, env=plugin_env, start_new_session=True,
                    check=False,
                )
                stdout_file.seek(0)
                stdout = stdout_file.read(MAX_PLUGIN_PAYLOAD + 1)
                stderr_file.seek(0, 2)
                stderr_file.seek(max(0, stderr_file.tell() - 400))
                stderr = stderr_file.read().decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning("Plugin %s failed for %s: %s", manifest["id"], hook, error)
            continue
        if len(stdout) > MAX_PLUGIN_PAYLOAD:
            LOGGER.warning("Ignoring oversized output from plugin %s", manifest["id"])
            continue
        output = stdout.decode("utf-8", errors="replace")
        if completed.returncode == 0 and output.strip():
            try:
                candidate = json.loads(output)
                if isinstance(candidate, dict):
                    result = candidate
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring invalid JSON from plugin %s", manifest["id"])
        elif completed.returncode:
            LOGGER.warning("Plugin %s exited with status %s: %s", manifest["id"], completed.returncode, stderr)
    return result
