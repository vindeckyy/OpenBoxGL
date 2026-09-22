"""Local OpenBox plugin packages and hooks.

The plugin surface is frozen as API v1 (see docs/plugin-api.md): manifest
hooks, palette commands, a bounded library read, and a notification post.
Everything else is internal and may change.
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path

from archives import safe_zip_extract
from backend_io import atomic_write_text


HOOKS = {"before_launch", "after_session", "library", "command", "library_source", "events"}
# Declared-permission whitelist for 1.14.0. A plugin that wants anything else
# is refused at manifest read time; grants never exceed declarations.
PERMISSIONS = frozenset({"network"})
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


def _sandboxed_command(package_root, entry, hook, *, share_net=False):
    """Build a bubblewrap command with no user-home or network access.

    ``share_net`` re-shares the host network namespace and is only passed for
    plugins the user explicitly granted the ``network`` permission. It must
    come after ``--unshare-all``: bubblewrap applies namespace flags in
    order, so a later ``--share-net`` clears the net bit ``--unshare-all``
    set without touching the other namespaces.
    """
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        return None
    relative_entry = entry.relative_to(package_root)
    command = [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if share_net:
        command.append("--share-net")
    command.extend([
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
    ])
    return command


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


def _plugin_command(package_root, entry, hook, *, trusted=False, share_net=False):
    if os.environ.get(UNSANDBOXED_PLUGINS_ENV) == "1":
        LOGGER.warning(
            "Running plugin %s without an OS sandbox because %s=1",
            package_root.name,
            UNSANDBOXED_PLUGINS_ENV,
        )
        return [sys.executable, "-B", str(RUNNER), str(entry), hook]
    command = _sandboxed_command(package_root, entry, hook, share_net=share_net)
    if command and _sandbox_available():
        return command
    if trusted:
        # Sandbox unavailable (e.g. Windows) but the user explicitly trusted
        # this exact package checksum: run unsandboxed, loudly.
        LOGGER.warning(
            "Running plugin %s without an OS sandbox: user-trusted package",
            package_root.name,
        )
        return [sys.executable, "-B", str(RUNNER), str(entry), hook]
    LOGGER.warning(
        "Skipping plugin %s because bubblewrap is unavailable; trust it in the Plugins manager to run it unsandboxed",
        package_root.name,
    )
    return None


def state_file(directory):
    return Path(directory) / "plugins-state.json"


def load_plugin_state(directory):
    try:
        data = json.loads(state_file(directory).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_plugin_state(directory, state):
    path = state_file(directory)
    atomic_write_text(path, json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Plugins 2.0: trust (F1a), permissions (F1b), settings (F1c)
# ---------------------------------------------------------------------------

def _clean_permissions(raw):
    """Validate the manifest ``permissions`` declaration (whitelist)."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("Plugin permissions must be a list of strings.")
    unknown = [item for item in raw if item not in PERMISSIONS]
    if unknown:
        raise ValueError(f"Plugin requests unknown permissions: {', '.join(sorted(unknown))}.")
    return sorted(set(raw))


SETTING_TYPES = ("string", "number", "integer", "boolean")


def _clean_settings_schema(raw):
    """Validate the manifest ``settings`` JSON-Schema subset (structural)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Plugin settings must be a JSON Schema object.")
    if raw.get("type", "object") != "object":
        raise ValueError("Plugin settings schema must be an object schema.")
    properties = raw.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("Plugin settings schema properties must be an object.")
    for name, field in properties.items():
        if not isinstance(field, dict):
            raise ValueError(f"Plugin setting {name!r} must be an object.")
        field_type = field.get("type", "string")
        if field_type not in SETTING_TYPES:
            raise ValueError(f"Plugin setting {name!r} has unsupported type {field_type!r}.")
        enum = field.get("enum")
        if enum is not None and (not isinstance(enum, list) or not enum):
            raise ValueError(f"Plugin setting {name!r} enum must be a non-empty list.")
    required = raw.get("required", [])
    if not isinstance(required, list) or any(name not in properties for name in required):
        raise ValueError("Plugin settings schema has an invalid required list.")
    return {"type": "object", "properties": properties, "required": list(required)}


def _check_setting_value(name, field, value):
    """Type/range-check one setting value; return the cleaned value."""
    field_type = field.get("type", "string")
    if field_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"Setting {name!r} must be a string.")
        if "minLength" in field and len(value) < int(field["minLength"]):
            raise ValueError(f"Setting {name!r} is too short.")
        if "maxLength" in field and len(value) > int(field["maxLength"]):
            raise ValueError(f"Setting {name!r} is too long.")
        return value
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Setting {name!r} must be true or false.")
        return value
    if field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Setting {name!r} must be a whole number.")
        number = value
    elif field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Setting {name!r} must be a number.")
        number = value
    else:  # pragma: no cover - guarded by _clean_settings_schema
        raise ValueError(f"Setting {name!r} has unsupported type {field_type!r}.")
    if "minimum" in field and number < field["minimum"]:
        raise ValueError(f"Setting {name!r} is below the minimum.")
    if "maximum" in field and number > field["maximum"]:
        raise ValueError(f"Setting {name!r} is above the maximum.")
    return number


def validate_plugin_settings(schema, values):
    """Hand-rolled validator for the settings JSON-Schema subset.

    Applies defaults, rejects unknown keys, and returns cleaned values.
    The runtime is dependency-free, so this stays stdlib-only by design.
    """
    schema = schema or {}
    properties = schema.get("properties", {})
    if not isinstance(values, dict):
        raise ValueError("Plugin settings must be an object.")
    unknown = sorted(name for name in values if name not in properties)
    if unknown:
        raise ValueError(f"Unknown plugin settings: {', '.join(unknown)}.")
    cleaned = {}
    for name, field in properties.items():
        if name in values:
            value = values[name]
        elif "default" in field:
            value = field["default"]
        elif name in schema.get("required", []):
            raise ValueError(f"Setting {name!r} is required.")
        else:
            continue
        enum = field.get("enum")
        if enum is not None and value not in enum:
            raise ValueError(f"Setting {name!r} must be one of: {', '.join(str(item) for item in enum)}.")
        cleaned[name] = _check_setting_value(name, field, value)
    return cleaned


def plugin_checksum(directory, plugin_id):
    """sha256 over the installed package (relative paths + contents).

    Computed at trust-prompt time for manual installs; a mismatch against the
    recorded checksum re-prompts, so updates cannot inherit old trust.
    """
    root = Path(directory) / str(plugin_id)
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    return digest.hexdigest()


def plugin_trust_status(directory, plugin_id):
    """Trust record for one plugin; trusted only when the checksum matches."""
    root = Path(directory)
    try:
        checksum = plugin_checksum(root, plugin_id)
    except OSError:
        return {"trusted": False, "checksum": "", "checksum_matches": False, "granted_at": ""}
    record = load_plugin_state(root).get("trust", {}).get(str(plugin_id))
    record = record if isinstance(record, dict) else {}
    checksum_matches = record.get("checksum") == checksum
    return {
        "trusted": bool(record.get("trusted") and checksum_matches),
        "checksum": checksum,
        "checksum_matches": bool(checksum_matches),
        "granted_at": str(record.get("granted_at") or ""),
    }


def set_plugin_trust(directory, plugin_id, trusted):
    """Grant or revoke unsandboxed-execution trust for one plugin.

    Grants are bound to the package checksum at grant time: any update
    changes the checksum and re-prompts. There is deliberately no
    trust-everything switch (ADR 0056).
    """
    root = Path(directory)
    manifest = read_manifest(root / str(plugin_id))
    state = load_plugin_state(root)
    trust = state.setdefault("trust", {})
    if trusted:
        trust[manifest["id"]] = {
            "trusted": True,
            "checksum": plugin_checksum(root, manifest["id"]),
            "granted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    else:
        trust.pop(manifest["id"], None)
    save_plugin_state(root, state)
    return bool(trusted)


def plugin_permission_grants(directory, plugin_id):
    """Permissions the user has granted this plugin (subset of its manifest)."""
    state = load_plugin_state(Path(directory))
    granted = (state.get("permissions") or {}).get(str(plugin_id), [])
    return sorted({item for item in granted if item in PERMISSIONS})


def set_plugin_permissions(directory, plugin_id, permissions):
    """Record a user grant; requested permissions must be declared."""
    root = Path(directory)
    manifest = read_manifest(root / str(plugin_id))
    wanted = {str(item) for item in (permissions or [])}
    unknown = wanted - PERMISSIONS
    if unknown:
        raise ValueError(f"Unknown permissions: {', '.join(sorted(unknown))}.")
    undeclared = wanted - set(manifest.get("permissions") or [])
    if undeclared:
        raise ValueError(f"Plugin does not declare permissions: {', '.join(sorted(undeclared))}.")
    state = load_plugin_state(root)
    grants = state.setdefault("permissions", {})
    grants[manifest["id"]] = sorted(wanted)
    save_plugin_state(root, state)
    return sorted(wanted)


def plugin_settings_values(directory, manifest):
    """Stored setting values merged over schema defaults (validated)."""
    schema = manifest.get("settings") or {}
    state = load_plugin_state(Path(directory))
    stored = (state.get("settings") or {}).get(manifest["id"], {})
    if not isinstance(stored, dict):
        stored = {}
    try:
        return validate_plugin_settings(schema, stored)
    except ValueError:
        # A schema update that invalidates stored values falls back to
        # defaults rather than breaking the plugin.
        return validate_plugin_settings(schema, {})


def get_plugin_settings(directory, plugin_id):
    """Return ``{"schema", "values"}`` for the Plugins dialog form."""
    manifest = read_manifest(Path(directory) / str(plugin_id))
    return {
        "schema": manifest.get("settings") or {"type": "object", "properties": {}},
        "values": plugin_settings_values(directory, manifest),
    }


def set_plugin_settings(directory, plugin_id, values):
    """Validate and store per-plugin settings."""
    root = Path(directory)
    manifest = read_manifest(root / str(plugin_id))
    cleaned = validate_plugin_settings(manifest.get("settings") or {}, values)
    state = load_plugin_state(root)
    settings = state.setdefault("settings", {})
    settings[manifest["id"]] = cleaned
    save_plugin_state(root, state)
    return cleaned


def emit_plugin_event(directory, event, payload=None):
    """Best-effort fan-out of a lifecycle event to ``events``-hook plugins.

    Never raises: a failing event path must not break the operation that
    emitted it (imports, session bookkeeping, shutdown).
    """
    if os.environ.get("OPENBOX_SAFE_MODE"):
        return
    try:
        body = {"event": event}
        if isinstance(payload, dict):
            body.update(payload)
        run_plugins(Path(directory), "events", body)
    except Exception:
        LOGGER.exception("Plugin lifecycle event %s failed", event)


# Fields compared for game_updated events: ids + changed field names only,
# never full objects, so payloads stay small (the 2MB cap stands).
EVENT_TRACKED_FIELDS = ("name", "platform", "progress", "favorite", "hidden", "rating", "playtime_seconds")
MAX_EVENT_IDS = 500
MAX_EVENT_UPDATES = 100


def snapshot_library(state):
    """Snapshot game ids to tracked-field tuples for add/remove/update diffs."""
    games = state.get("games", []) if isinstance(state, dict) else []
    snapshot = {}
    for game in games:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "")
        if not game_id:
            continue
        snapshot[game_id] = tuple(
            game.get("playtime_seconds", 0) if field == "playtime_seconds"
            else bool(game.get(field)) if field in ("favorite", "hidden")
            else str(game.get(field) or "")
            for field in EVENT_TRACKED_FIELDS
        )
    return snapshot


def emit_library_diff(directory, before, after):
    """Diff two library snapshots and emit game_added/removed/updated."""
    if before is None or after is None:
        return
    added = [game_id for game_id in after if game_id not in before][:MAX_EVENT_IDS]
    removed = [game_id for game_id in before if game_id not in after][:MAX_EVENT_IDS]
    updated = []
    for game_id, fingerprint in after.items():
        if game_id in before and fingerprint != before[game_id]:
            changed = [
                field for field, old, new in zip(EVENT_TRACKED_FIELDS, before[game_id], fingerprint, strict=True)
                if old != new
            ]
            if changed:
                updated.append({"game_id": game_id, "changed": changed})
            if len(updated) >= MAX_EVENT_UPDATES:
                break
    if added:
        emit_plugin_event(directory, "game_added", {"game_ids": added})
    if removed:
        emit_plugin_event(directory, "game_removed", {"game_ids": removed})
    if updated:
        emit_plugin_event(directory, "game_updated", {"changes": updated})


def merge_library_source_games(games, data_parent):
    """Run ``library_source`` importer plugins and merge their games (F1d).

    Imported games get plugin-namespaced ids (``plugin:<id>:<their_id>``) and
    a ``plugin_source`` provenance field, so disabling or removing the plugin
    drops its games on the next public-state rebuild. Entries are validated
    to the canonical shape with the same cleaner the library routes use.
    """
    from handlers.library import _clean_game_fields  # lazy: handlers import webapp_state

    data_parent = Path(data_parent)
    plugins_dir = data_parent / "plugins"
    seen = {str(game.get("game_id") or "") for game in games if isinstance(game, dict)}
    merged = []
    for manifest in list_plugins(plugins_dir):
        if not manifest.get("valid") or not manifest.get("enabled"):
            continue
        if "library_source" not in manifest.get("hooks", []):
            continue
        result, error = run_plugin_hook(plugins_dir, manifest["id"], "library_source", {"api_version": PLUGIN_API_VERSION})
        if error or not isinstance(result, dict):
            if error:
                LOGGER.warning("Plugin %s library_source failed: %s", manifest["id"], error)
            continue
        entries = result.get("games")
        if not isinstance(entries, list):
            LOGGER.warning("Plugin %s library_source must return {games: [...]}.", manifest["id"])
            continue
        for entry in entries[:MAX_LIBRARY_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            try:
                game = _clean_game_fields(entry)
            except (ValueError, TypeError, AttributeError) as validation_error:
                LOGGER.warning("Plugin %s library_source entry rejected: %s", manifest["id"], validation_error)
                continue
            their_id = str(entry.get("id") or entry.get("game_id") or game.get("name") or "")[:96]
            namespaced = f"plugin:{manifest['id']}:{their_id}"[:160]
            if not their_id or namespaced in seen:
                continue
            seen.add(namespaced)
            game["game_id"] = namespaced
            game["plugin_source"] = manifest["id"]
            game["plugin_source_name"] = manifest.get("name") or manifest["id"]
            merged.append(game)
    base = len(games)
    for offset, game in enumerate(merged):
        game["id"] = base + offset
    return games + merged


def read_manifest(path):
    try:
        manifest = json.loads((path / "plugin.json").read_text(encoding="utf-8"))
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
    permissions = _clean_permissions(manifest.get("permissions"))
    settings = _clean_settings_schema(manifest.get("settings"))
    return {
        **manifest,
        "id": plugin_id,
        "entry": entry,
        "hooks": hooks,
        "api_version": api_version,
        "commands": commands,
        "permissions": permissions,
        "settings": settings,
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
        trust = plugin_trust_status(root, manifest["id"])
        plugins.append({
            **manifest,
            "valid": True,
            "enabled": manifest["id"] not in disabled,
            "sandbox": sandbox,
            "checksum": trust["checksum"],
            "trusted": trust["trusted"],
            "granted_permissions": plugin_permission_grants(root, manifest["id"]),
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
    # Per-plugin settings travel in the stdin JSON payload — the sanctioned
    # channel that sidesteps the environment scrubbing (F1c).
    if manifest.get("settings"):
        payload = {**(payload if isinstance(payload, dict) else {}), "settings": plugin_settings_values(root, manifest)}
    encoded = json.dumps(payload)
    if len(encoded.encode("utf-8")) > MAX_PLUGIN_PAYLOAD:
        return None, "Payload is too large for plugin execution."
    trust = plugin_trust_status(root, manifest["id"])["trusted"]
    share_net = "network" in plugin_permission_grants(root, manifest["id"])
    command = _plugin_command(package_root, entry, hook, trusted=trust, share_net=share_net)
    if command is None:
        return None, "bubblewrap is unavailable; trust this plugin in the Plugins manager to run it unsandboxed."
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
    # Clear any disabled state so a reinstall comes back enabled, and drop
    # the plugin's trust, permission, and settings records: a reinstall must
    # re-prompt rather than inherit the previous install's approvals.
    state = load_plugin_state(root)
    disabled = set(state.get("disabled", []))
    disabled.discard(manifest["id"])
    state["disabled"] = sorted(disabled)
    for section in ("trust", "permissions", "settings"):
        records = state.get(section)
        if isinstance(records, dict):
            records.pop(manifest["id"], None)
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
