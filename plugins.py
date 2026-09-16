"""Local OpenBox plugin packages and hooks.

The plugin surface is frozen as API v1 (see docs/plugin-api.md): manifest
hooks, palette commands, a bounded library read, and a notification post.
Everything else is internal and may change.
"""

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


HOOKS = {"before_launch", "after_session", "library", "command"}
PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
COMMAND_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PLUGIN_API_VERSION = 1
RUNNER = Path(__file__).with_name("plugin_runner.py")
LOGGER = logging.getLogger("openbox.plugins")
MAX_PLUGIN_PAYLOAD = 2 * 1024 * 1024
MAX_COMMANDS = 32
MAX_LIBRARY_ENTRIES = 500
UNSANDBOXED_PLUGINS_ENV = "OPENBOX_ALLOW_UNSANDBOXED_PLUGINS"

def sandbox_status():
    """Report the sandbox mode the host will use for plugin execution."""
    if os.environ.get(UNSANDBOXED_PLUGINS_ENV) == "1":
        return "disabled"
    if not shutil.which("bwrap"):
        return "unavailable"
    try:
        return "ready" if _sandbox_available() else "unavailable"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # A mocked or broken subprocess layer must fail toward "unavailable"
        # (skip the plugin) rather than break session bookkeeping.
        return "unavailable"


def _clean_commands(raw, plugin_id):
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_COMMANDS:
        raise ValueError("Plugin commands must be a bounded list.")
    commands = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Plugin commands must be objects with id and label.")
        command_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        if not COMMAND_ID.fullmatch(command_id) or not label:
            raise ValueError("Plugin commands need a valid id and a label.")
        if command_id in seen:
            raise ValueError("Plugin command ids must be unique.")
        seen.add(command_id)
        commands.append({
            "id": command_id,
            "label": label[:80],
            "description": str(item.get("description") or "")[:280],
            "plugin_id": plugin_id,
            "source": "manifest",
        })
    return commands



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
    api_version = manifest.get("api_version", PLUGIN_API_VERSION)
    if isinstance(api_version, bool) or not isinstance(api_version, int) or api_version < 1:
        raise ValueError("Plugin api_version must be a positive integer.")
    if api_version > PLUGIN_API_VERSION:
        raise ValueError(f"Plugin requires API v{api_version}; this build supports v{PLUGIN_API_VERSION}.")
    entry_path = (path / entry).resolve()
    if path.resolve() not in entry_path.parents or not entry_path.is_file() or entry_path.suffix != ".py":
        raise ValueError("Plugin entry must be a Python file inside the package.")
    commands = _clean_commands(manifest.get("commands"), plugin_id)
    return {
        **manifest,
        "id": plugin_id,
        "entry": entry,
        "hooks": hooks,
        "api_version": api_version,
        "commands": commands,
    }


def list_plugins(directory):
    """List installed plugins, including malformed packages that fail validation.

    Malformed and unsandboxed plugins are surfaced with ``valid: False`` and a
    sandbox status instead of being hidden, so the manager can explain why a
    plugin cannot run.
    """
    root = Path(directory)
    disabled = set(load_plugin_state(root).get("disabled", []))
    candidates = []
    if root.is_dir():
        candidates = [
            path for path in sorted(root.iterdir())
            if path.is_dir() and not path.name.startswith(".")
        ]
    sandbox = sandbox_status() if candidates else "ready"
    plugins = []
    for path in candidates:
        try:
            manifest = read_manifest(path)
        except ValueError as error:
            plugins.append({
                "id": path.name,
                "name": path.name,
                "version": "",
                "valid": False,
                "error": str(error),
                "enabled": False,
                "sandbox": sandbox,
                "commands": [],
                "hooks": [],
            })
            continue
        plugins.append({
            **manifest,
            "valid": True,
            "enabled": manifest["id"] not in disabled,
            "sandbox": sandbox,
        })
    return plugins


def plugin_commands(directory, *, include_disabled=False):
    """Return palette commands declared by valid plugin manifests."""
    commands = []
    for manifest in list_plugins(directory):
        if not manifest.get("valid") or (not include_disabled and not manifest.get("enabled")):
            continue
        commands.extend(manifest.get("commands") or [])
    return commands


def run_plugin_hook(directory, plugin_id, hook, payload):
    """Run one plugin hook and return ``(result, error)``.

    ``error`` is a human-readable reason when the plugin was skipped (invalid
    plugin id, unsandboxed host, oversized payload, missing runner). The result
    is the parsed dict output or None.
    """
    root = Path(directory)
    try:
        manifest = read_manifest(root / str(plugin_id))
    except ValueError as error:
        return None, str(error)
    if hook not in manifest["hooks"]:
        return None, f"Plugin does not declare the {hook} hook."
    return _run_manifest_hook(root, manifest, hook, payload)


def _run_manifest_hook(root, manifest, hook, payload):
    package_root = root / manifest["id"]
    entry = package_root / manifest["entry"]
    encoded = json.dumps(payload)
    if len(encoded.encode("utf-8")) > MAX_PLUGIN_PAYLOAD:
        return None, "Payload is too large for plugin execution."
    command = _plugin_command(package_root, entry, hook)
    if command is None:
        return None, "bubblewrap is unavailable; the plugin was not run unsandboxed."
    try:
        plugin_env = _plugin_environment()
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
        return None, str(error)
    if len(stdout) > MAX_PLUGIN_PAYLOAD:
        return None, "Plugin output exceeded the payload limit."
    if completed.returncode:
        return None, f"Plugin exited with status {completed.returncode}: {stderr}"
    output = stdout.decode("utf-8", errors="replace")
    if not output.strip():
        return None, ""
    try:
        candidate = json.loads(output)
    except json.JSONDecodeError:
        return None, "Plugin returned invalid JSON."
    if not isinstance(candidate, dict):
        return None, "Plugin output must be a JSON object."
    return candidate, ""


def _plugin_environment():
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
    return plugin_env


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
    root = Path(directory)
    result = payload
    for manifest in list_plugins(root):
        if not manifest.get("valid") or not manifest["enabled"] or hook not in manifest["hooks"]:
            continue
        candidate, error = _run_manifest_hook(root, manifest, hook, result)
        if candidate is not None:
            result = candidate
        elif error:
            LOGGER.warning("Plugin %s failed for %s: %s", manifest["id"], hook, error)
    return result
