"""Per-emulator settings schemas, launch-argument application, and version probes.

The settings model is intentionally small: only the values OpenBox itself knows
how to translate into an emulator command line are modelled here.  A launcher
that cannot write a setting must not offer it, or the option silently does
nothing.

``apply_to_command`` never raises for IO problems: a launch must not be blocked
because a generated RetroArch config could not be written.  It returns the
original argv and records the reason in ``meta["warnings"]`` instead.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend_io import atomic_write_text  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[2]

SETTINGS_SCHEMAS = {
    "mame": {
        "label": "MAME",
        "fields": {
            "monitor": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
            "borderless": {"type": "boolean", "default": False},
            "screen_effect": {"type": "enum", "values": ["none", "scanlines"], "default": "none"},
            "lightgun": {"type": "boolean", "default": False},
            "trackball": {"type": "boolean", "default": False},
        },
    },
    "retroarch": {
        "label": "RetroArch",
        "fields": {
            "display_mode": {
                "type": "enum",
                "values": ["windowed", "fullscreen", "borderless"],
                "default": "fullscreen",
            },
            "screen_effect": {"type": "enum", "values": ["none", "scanlines"], "default": "none"},
            "run_ahead": {"type": "integer", "minimum": 0, "maximum": 3, "default": 0},
            "rewind": {"type": "boolean", "default": False},
            "upscale": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
        },
    },
}

# The registry identifies emulators by their Flatpak application id, while the
# settings schemas use the short names the UI and plan use.  Accept both so a
# caller can pass whatever it has without duplicating the schema.
EMULATOR_ALIASES = {
    "mame": "mame",
    "org.mamedev.mame": "mame",
    "retroarch": "retroarch",
    "org.libretro.retroarch": "retroarch",
}

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}

_VERSION_CACHE_TTL = 60.0
_VERSION_CACHE: dict[str, tuple[float, dict]] = {}
_VERSION_CACHE_LOCK = threading.Lock()


def canonical_emulator_id(emulator_id):
    """Return the schema key for *emulator_id* or raise ``ValueError``."""
    key = str(emulator_id or "").strip()
    canonical = EMULATOR_ALIASES.get(key.lower())
    if canonical is None or canonical not in SETTINGS_SCHEMAS:
        raise ValueError(f"Unknown emulator: {emulator_id or '(empty)'}")
    return canonical


def _schema_for(emulator_id):
    canonical = canonical_emulator_id(emulator_id)
    return canonical, SETTINGS_SCHEMAS[canonical]


def _coerce_integer(field, value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer.") from None
    if number < field["minimum"] or number > field["maximum"]:
        raise ValueError(
            f"{name} must be between {field['minimum']} and {field['maximum']}."
        )
    return number


def _coerce_boolean(field, value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"{name} must be true or false.")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    raise ValueError(f"{name} must be true or false.")


def _coerce_enum(field, value, name):
    if not isinstance(value, str) or value not in field["values"]:
        allowed = ", ".join(field["values"])
        raise ValueError(f"{name} must be one of: {allowed}.")
    return value


def _coerce_field(field, value, name):
    kind = field["type"]
    if kind == "integer":
        return _coerce_integer(field, value, name)
    if kind == "boolean":
        return _coerce_boolean(field, value, name)
    if kind == "enum":
        return _coerce_enum(field, value, name)
    raise ValueError(f"{name} has an unsupported type.")


def normalize_values(emulator_id, values):
    """Validate *values* and return only the fields that were provided.

    Unknown emulators and unknown fields are rejected rather than ignored: a
    typo in a setting name should be visible, not silently stored.
    """
    _canonical, schema = _schema_for(emulator_id)
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("Emulator settings values must be an object.")
    fields = schema["fields"]
    cleaned = {}
    for name, value in values.items():
        field = fields.get(name)
        if field is None:
            raise ValueError(f"Unknown setting for {schema['label']}: {name}")
        cleaned[name] = _coerce_field(field, value, name)
    return cleaned


def _entry_keys(emulator_id, canonical):
    keys = [str(emulator_id), canonical]
    keys.extend(alias for alias, target in EMULATOR_ALIASES.items() if target == canonical)
    return list(dict.fromkeys(keys))


def _lookup_entry(entries, keys):
    for key in keys:
        entry = entries.get(key)
        if isinstance(entry, dict):
            return entry
    wanted = {key.lower() for key in keys}
    for key, entry in entries.items():
        if isinstance(entry, dict) and str(key).lower() in wanted:
            return entry
    return {}


def _settings_entry(state, emulator_id):
    canonical = canonical_emulator_id(emulator_id)
    if not isinstance(state, dict):
        return {}
    entries = state.get("emulator_settings")
    if not isinstance(entries, dict):
        return {}
    return _lookup_entry(entries, _entry_keys(emulator_id, canonical))


def _stored_values(schema, raw):
    """Coerce stored values defensively; bad/unknown values are ignored."""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for name, value in raw.items():
        field = schema["fields"].get(name)
        if field is None:
            continue
        try:
            cleaned[name] = _coerce_field(field, value, name)
        except ValueError:
            continue
    return cleaned


def effective_values(state, emulator_id, platform):
    """Return schema defaults overlaid by global and platform values."""
    _canonical, schema = _schema_for(emulator_id)
    values = {name: field["default"] for name, field in schema["fields"].items()}
    entry = _settings_entry(state, emulator_id)
    values.update(_stored_values(schema, entry.get("global")))
    platform = str(platform or "")
    if platform:
        platforms = entry.get("platforms")
        if isinstance(platforms, dict):
            values.update(_stored_values(schema, platforms.get(platform)))
    return values


def is_managed(state, emulator_id):
    """True when OpenBox is responsible for writing this emulator's settings."""
    canonical = canonical_emulator_id(emulator_id)
    if not isinstance(state, dict):
        return False
    entries = state.get("emulator_settings")
    if not isinstance(entries, dict):
        return False
    entry = _lookup_entry(entries, _entry_keys(emulator_id, canonical))
    return entry.get("managed") is True


def _rom_index(argv):
    """Index before the final ROM/path token.

    If the last token looks like a flag there is no ROM token to insert
    before, so the new flags are appended.
    """
    if len(argv) < 2:
        return len(argv)
    if str(argv[-1]).startswith("-"):
        return len(argv)
    return len(argv) - 1


def _insert_before_rom(argv, flags):
    if not flags:
        return list(argv)
    index = _rom_index(argv)
    return list(argv[:index]) + list(flags) + list(argv[index:])


def _mame_argv(argv, values, warnings):
    flags = ["-screen", str(values["monitor"])]
    if values["borderless"]:
        flags.append("-borderless")
    if values["screen_effect"] == "scanlines":
        effect = APP_ROOT / "assets" / "mame" / "scanlines.png"
        if effect.is_file():
            flags.extend(["-effect", str(effect)])
        else:
            warnings.append("MAME scanlines were requested but the bundled effect image is missing.")
    flags.append("-lightgun" if values["lightgun"] else "-nolightgun")
    flags.append("-trackball" if values["trackball"] else "-notrackball")
    return _insert_before_rom(argv, flags)


def _safe_platform(platform):
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(platform or "")).strip("-._")
    return text.lower() or "default"


def retroarch_config_path(data_dir, platform):
    """Path of the generated ``--appendconfig`` file for *platform*."""
    return Path(data_dir).expanduser() / "emulator-configs" / "retroarch" / f"{_safe_platform(platform)}.cfg"


def _retroarch_settings(values, warnings):
    settings = {}
    mode = values["display_mode"]
    if mode == "fullscreen":
        settings["video_fullscreen"] = "true"
    elif mode == "windowed":
        settings["video_fullscreen"] = "false"
    elif mode == "borderless":
        settings["video_windowed_fullscreen"] = "true"

    if values["screen_effect"] == "scanlines":
        shader = APP_ROOT / "assets" / "shaders" / "scanlines.slangp"
        if shader.is_file():
            settings["video_shader_enable"] = "true"
            settings["video_shader"] = str(shader)
        else:
            warnings.append("RetroArch scanlines were requested but the bundled shader is missing.")
    else:
        settings["video_shader_enable"] = "false"

    run_ahead = values["run_ahead"]
    if run_ahead:
        settings["run_ahead_enabled"] = "true"
        settings["run_ahead_frames"] = str(run_ahead)
    else:
        settings["run_ahead_enabled"] = "false"
    settings["rewind_enable"] = "true" if values["rewind"] else "false"
    settings["video_scale"] = str(values["upscale"])
    return settings


def _attach_appendconfig(argv, config_path):
    path = str(config_path)
    updated = list(argv)
    for index, argument in enumerate(updated):
        if argument == "--appendconfig":
            if index + 1 < len(updated):
                existing = str(updated[index + 1])
                updated[index + 1] = f"{existing},{path}" if existing else path
            else:
                updated.append(path)
            return updated
        if isinstance(argument, str) and argument.startswith("--appendconfig="):
            existing = argument.split("=", 1)[1]
            updated[index] = f"--appendconfig={existing},{path}" if existing else f"--appendconfig={path}"
            return updated
    index = _rom_index(updated)
    return list(updated[:index]) + ["--appendconfig", path] + list(updated[index:])


def _retroarch_argv(argv, values, data_dir, meta):
    warnings = meta["warnings"]
    if not str(data_dir or "").strip():
        warnings.append("RetroArch settings were skipped because no data directory was provided.")
        return list(argv)
    settings = _retroarch_settings(values, warnings)
    path = retroarch_config_path(data_dir, values.get("_platform", ""))
    text = "".join(f'{key} = "{value}"\n' for key, value in settings.items())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text, mode=0o600)
    except OSError as error:
        warnings.append(f"RetroArch settings could not be written: {error}")
        return list(argv)
    meta["created_files"].append(str(path))
    return _attach_appendconfig(argv, path)


def apply_to_command(emulator_id, platform, argv, state, data_dir):
    """Apply stored settings to *argv*.

    Returns ``(argv, env, meta)``.  ``meta`` always carries ``created_files``
    and ``warnings`` lists.  Unsupported emulators and IO failures return the
    original argv unchanged.  This applies built-in sections only; the launch
    path calls :func:`apply_plugin_settings` separately for plugin sections.
    """
    meta = {"created_files": [], "warnings": []}
    original = list(argv or [])
    try:
        canonical = canonical_emulator_id(emulator_id)
    except ValueError:
        meta["warnings"].append(f"Unknown emulator settings: {emulator_id or '(empty)'}")
        return original, {}, meta
    values = effective_values(state, emulator_id, platform)
    if canonical == "mame":
        return _mame_argv(original, values, meta["warnings"]), {}, meta
    values["_platform"] = str(platform or "")
    return _retroarch_argv(original, values, data_dir, meta), {}, meta


PLUGIN_SETTINGS_PREFIX = "plugin:"
_PLUGIN_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_PLUGIN_FORBIDDEN_ENV = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"}


def plugin_section_key(plugin_id, emulator_id):
    """Return the state key for one plugin emulator section."""
    return f"{PLUGIN_SETTINGS_PREFIX}{plugin_id}:{emulator_id}"


def plugin_emulator_sections(state, plugins_dir=None):
    """Yield plugin emulator sections declared by enabled plugins."""
    if not isinstance(state, dict):
        return []
    from plugins import list_plugins

    entries = state.get("emulator_settings")
    if not isinstance(entries, dict):
        entries = {}
    sections = []
    for manifest in list_plugins(Path(plugins_dir) if plugins_dir else _default_plugins_dir()):
        if not manifest.get("valid") or not manifest.get("enabled"):
            continue
        capabilities = manifest.get("capabilities") or {}
        declarations = capabilities.get("emulator_settings") or []
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            emulator_id = str(declaration.get("emulator_id") or "").strip()
            if not emulator_id:
                continue
            key = plugin_section_key(manifest["id"], emulator_id)
            stored = entries.get(key)
            sections.append({
                "plugin_id": manifest["id"],
                "plugin_name": manifest.get("name", manifest["id"]),
                "emulator_id": emulator_id,
                "key": key,
                "label": declaration.get("label", emulator_id),
                "schema": declaration.get("schema", {}),
                "scopes": declaration.get("scopes", []),
                "managed": bool(stored.get("managed")) if isinstance(stored, dict) else False,
                "global": stored.get("global", {}) if isinstance(stored, dict) else {},
                "platforms": stored.get("platforms", {}) if isinstance(stored, dict) else {},
            })
    return sections


def _default_plugins_dir():
    from openbox import DATA

    return Path(DATA).parent / "plugins"


def plugin_effective_values(section, platform):
    """Schema defaults plus stored global/platform values for one section."""
    from plugins import validate_plugin_settings

    schema = section.get("schema") or {}
    values = validate_plugin_settings(schema, {})
    values.update(validate_plugin_settings(schema, section.get("global") or {}))
    if platform:
        platforms = section.get("platforms") or {}
        raw_platform = platforms.get(platform) or {}
        if isinstance(raw_platform, dict) and raw_platform:
            cleaned = validate_plugin_settings(schema, {
                key: value for key, value in raw_platform.items()
                if key in (schema.get("properties") or {})
            })
            values.update({key: value for key, value in cleaned.items() if key in raw_platform})
    return values


def _clean_plugin_args(raw):
    if not isinstance(raw, list) or len(raw) > 32:
        raise ValueError("Plugin emulator args must be a list of at most 32 strings.")
    cleaned = []
    for value in raw:
        if not isinstance(value, str) or len(value) > 500 or "\0" in value:
            raise ValueError("Plugin emulator args must be strings of at most 500 characters.")
        cleaned.append(value)
    return cleaned


def _clean_plugin_env(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 32:
        raise ValueError("Plugin emulator env must be an object with at most 32 keys.")
    cleaned = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _PLUGIN_ENV_NAME.fullmatch(key):
            raise ValueError("Plugin emulator env keys must be uppercase identifiers.")
        if key in _PLUGIN_FORBIDDEN_ENV:
            raise ValueError(f"Plugin emulator env may not set {key}.")
        if not isinstance(value, str) or len(value) > 500 or "\0" in value:
            raise ValueError("Plugin emulator env values must be strings of at most 500 characters.")
        cleaned[key] = value
    return cleaned


def apply_plugin_settings(emulator_id, platform, argv, state, data_dir, warnings):
    """Run enabled plugin ``emulator`` hooks and merge their validated args/env."""
    from plugins import run_plugin_hook

    root = Path(data_dir) / "plugins" if data_dir else _default_plugins_dir()
    updated = list(argv)
    env = {}
    for section in plugin_emulator_sections(state, root):
        if section["emulator_id"] != emulator_id or not section["managed"]:
            continue
        try:
            values = plugin_effective_values(section, platform)
        except ValueError as error:
            warnings.append(f"Plugin emulator settings for {section['label']} were ignored: {error}")
            continue
        result, error = run_plugin_hook(root, section["plugin_id"], "emulator", {
            "action": "apply",
            "emulator_id": section["emulator_id"],
            "platform": str(platform or ""),
            "values": values,
            "argv": list(updated),
        })
        if error:
            warnings.append(f"Plugin emulator settings for {section['label']} were skipped: {error}")
            continue
        if not isinstance(result, dict):
            continue
        try:
            args = _clean_plugin_args(result.get("args", []))
            plugin_env = _clean_plugin_env(result.get("env"))
        except ValueError as error:
            warnings.append(f"Plugin emulator settings for {section['label']} were ignored: {error}")
            continue
        updated = _insert_before_rom(updated, args)
        env.update(plugin_env)
    return updated, env


def version_probe(emulator_id, definition=None, timeout=5.0, *, which=shutil.which, run=subprocess.run, now=None):
    """Best-effort installed/latest version information for one emulator.

    Native executables are only run with ``--version`` and always with a
    timeout.  Flatpak probes use ``flatpak info --show-version`` (short) and
    ``flatpak remote-info`` (longer).  Results are cached in-process for 60
    seconds because the emulator dialog and status endpoint ask repeatedly.
    """
    key = str(emulator_id or "")
    current = float(now() if now is not None else time.monotonic())
    with _VERSION_CACHE_LOCK:
        cached = _VERSION_CACHE.get(key)
        if cached is not None and current - cached[0] < _VERSION_CACHE_TTL:
            return dict(cached[1])
    result = _probe_version(key, definition or {}, timeout=timeout, which=which, run=run)
    with _VERSION_CACHE_LOCK:
        _VERSION_CACHE[key] = (current, dict(result))
    return result


def _run_capture(run, command, timeout):
    """Run a probe command; return ``(ok, text, error)`` without raising."""
    try:
        completed = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout:g}s"
    except (OSError, subprocess.SubprocessError) as error:
        return False, "", str(error)
    text = (getattr(completed, "stdout", "") or "") + (getattr(completed, "stderr", "") or "")
    if getattr(completed, "returncode", 1) != 0:
        return False, text, _first_line(getattr(completed, "stderr", "") or "") or "probe failed"
    return True, text, ""


def _first_line(text):
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _parse_remote_version(text):
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("version:"):
            return stripped.split(":", 1)[1].strip()
    return _first_line(text)


def _probe_version(emulator_id, definition, *, timeout, which, run):
    app_id = str(
        definition.get("app_id")
        or definition.get("flatpak_app_id")
        or definition.get("flatpak")
        or emulator_id
        or ""
    ).strip()
    native_name = str(definition.get("native") or definition.get("native_exe") or "").strip()
    flatpak = which("flatpak") if which else None
    native_binary = which(native_name) if (which and native_name) else None

    version = ""
    latest_version = ""
    errors = []
    flatpak_installed = False

    if native_binary:
        ok, text, error = _run_capture(run, [native_binary, "--version"], timeout)
        if ok:
            version = _first_line(text)
        elif error:
            errors.append(error)

    if flatpak and app_id:
        if not native_binary:
            ok, text, error = _run_capture(
                run, [flatpak, "info", "--user", "--show-version", app_id], timeout
            )
            flatpak_installed = ok
            if ok:
                version = _first_line(text)
            elif error:
                errors.append(error)
        ok, text, error = _run_capture(
            run,
            [flatpak, "remote-info", "--user", "flathub", app_id],
            timeout * 2,
        )
        if ok:
            latest_version = _parse_remote_version(text)
        elif error:
            errors.append(error)

    installed = bool(native_binary or flatpak_installed)
    update_available = bool(version and latest_version and version.strip() != latest_version.strip())
    return {
        "installed": installed,
        "native": bool(native_binary),
        "flatpak": bool(flatpak_installed),
        "version": version,
        "latest_version": latest_version,
        "update_available": update_available,
        "error": "; ".join(errors),
    }


def _reset_version_cache():
    """Clear the in-process version cache (tests and post-install refresh)."""
    with _VERSION_CACHE_LOCK:
        _VERSION_CACHE.clear()


__all__ = [
    "SETTINGS_SCHEMAS",
    "apply_to_command",
    "canonical_emulator_id",
    "effective_values",
    "emulator_version_status",
    "is_managed",
    "normalize_values",
    "retroarch_config_path",
    "version_probe",
]

# The plan names the probe ``emulator_version_status``; keep both spellings
# pointing at the same implementation so the handler can choose either.
emulator_version_status = version_probe
