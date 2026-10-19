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
from urllib.parse import urlsplit

from archives import safe_zip_extract
from backend_io import atomic_write_text
from pkg.platform_compat import resolved_data_dir


# Capability declarations are additive on top of the frozen v1 hook set
# (ADR 0077). Each capability name maps to the hook a plugin must declare;
# the host refuses a capability whose hook is missing.
CAPABILITY_HOOKS = {
    "storefront": "storefront",
    "metadata": "metadata",
    "media": "media",
    "stats": "stats",
    "emulator_settings": "emulator",
    "discovery": "discovery",
}
CAPABILITY_HOOK_NAMES = frozenset(CAPABILITY_HOOKS.values())
# Hooks that receive the data bag, capability settings, and credentials.
PRIVILEGED_HOOK_NAMES = frozenset(CAPABILITY_HOOK_NAMES)
HOOKS = (
    {"before_launch", "after_session", "library", "command", "library_source", "events"}
    | set(PRIVILEGED_HOOK_NAMES)
)
# Per-hook wall-clock budgets. Legacy hooks keep the historical 5 s.
HOOK_TIMEOUTS = {
    "storefront": 30,
    "metadata": 15,
    "media": 15,
    "stats": 15,
    "emulator": 10,
    "discovery": 10,
    "default": 5,
}
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
MAX_PLUGIN_DATA = 1024 * 1024
MAX_PLUGIN_DATA_KEY = 128
MAX_PLUGIN_DATA_DEPTH = 4
MAX_CREDENTIAL_KEYS = 128
MAX_CREDENTIAL_VALUE = 4096
MAX_CAPABILITY_NAME = 80
MAX_CAPABILITY_PLATFORM = 40
MAX_CAPABILITY_MEDIA_KINDS = 20
MAX_EMULATOR_CAPABILITIES = 8
MAX_MESSAGE_KEYS = 64
MAX_MESSAGE_TEXT = 500
MESSAGE_LOCALES = ("en", "es", "de", "fr", "pt")
NOTIFICATION_LEVELS = frozenset({"info", "success", "warning", "error"})
CAPABILITY_ID = PLUGIN_ID
EMULATOR_CAPABILITY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MESSAGE_KEY = re.compile(r"^[a-z0-9._-]{1,64}$")
UNSANDBOXED_PLUGINS_ENV = "OPENBOX_ALLOW_UNSANDBOXED_PLUGINS"


def _capability_media_kinds():
    """Media kinds a metadata/media capability may declare.

    Imported lazily: plugins.py is imported very early by web_app, and the
    media field tables pull in state modules that must not import plugins.
    """
    try:
        from pkg.state.media_probe import MEDIA_PATH_FIELDS, MEDIA_TYPES_ALL
        return frozenset(MEDIA_PATH_FIELDS) | frozenset(MEDIA_TYPES_ALL) | {"screenshots"}
    except Exception:  # an unavailable media module must not block manifests
        return frozenset({
            "cover", "background", "screenshots", "clear_logo", "fanart",
            "banner", "icon", "box_back", "box_spine", "box_3d",
            "title_screen", "cart_front", "cart_back", "disc", "advertisement",
            "manual", "video", "music", "video_snap", "video_theme",
            "video_trailer",
        })


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


#: Top-level directories the sandbox already masks. The data directory is
#: usually under /home and therefore covered by accident rather than by
#: design; see _sandboxed_command.
SANDBOX_MASKED_PREFIXES = ("/home", "/tmp", "/run", "/mnt", "/media")


def _needs_data_dir_mask(data_posix: str) -> bool:
    """True when the data directory is not already hidden by a --tmpfs.

    Kept as a pure function of a POSIX path string so the prefix rule can be
    tested on any platform. ``Path.resolve()`` on Windows prepends a drive
    letter, so a test that fed it "/srv/data" would be asserting about a path
    that cannot exist on the host running the test.
    """
    return not any(
        data_posix == prefix or data_posix.startswith(prefix + "/")
        for prefix in SANDBOX_MASKED_PREFIXES
    )


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
    # Mask the data directory explicitly.
    #
    # The five --tmpfs entries below only protect the data directory *by
    # accident*: it normally lives under $HOME, and /home is masked. But
    # OPENBOX_DATA_DIR is a documented, supported override, and
    # default_data_dir() falls back to the process cwd when the home directory
    # cannot be determined. Either way, a data directory outside those five
    # paths stays readable through the --ro-bind of the whole filesystem --
    # library.json, settings.json (which holds provider credentials) and the
    # plugin state.
    #
    # Masking it by resolved path is free, because the plugin contract already
    # gives a plugin everything it is entitled to on stdin: plugin_runner.py
    # does json.load(sys.stdin) and plugins.py documents per-plugin settings as
    # travelling in that payload. No legitimate plugin reads the data directory
    # from disk, and this makes the documented guarantee true.
    data_dir = resolved_data_dir().resolve()
    command.extend([
        "--ro-bind", "/", "/",
        "--tmpfs", "/home",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/mnt",
        "--tmpfs", "/media",
        "--proc", "/proc",
        "--dev", "/dev",
    ])
    # Only add the mask when the directory is not already covered by one of the
    # tmpfs entries above; a redundant --tmpfs would needlessly hide it twice.
    if _needs_data_dir_mask(data_dir.as_posix()):
        command.extend(["--tmpfs", data_dir.as_posix()])
    command.extend([
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


def _clean_messages(raw):
    """Validate the optional localized notification table."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_MESSAGE_KEYS:
        raise ValueError(f"Plugin messages must be an object with at most {MAX_MESSAGE_KEYS} keys.")
    messages = {}
    for key, translations in raw.items():
        if not isinstance(key, str) or not MESSAGE_KEY.fullmatch(key):
            raise ValueError(f"Plugin message key {key!r} is invalid.")
        if not isinstance(translations, dict):
            raise ValueError(f"Plugin message {key!r} must map locales to text.")
        clean = {}
        for locale, text in translations.items():
            if locale not in MESSAGE_LOCALES:
                # Forward compatible: a future locale is dropped, not fatal.
                continue
            if not isinstance(text, str):
                raise ValueError(f"Plugin message {key!r} has a non-text {locale!r} translation.")
            clean[locale] = text[:MAX_MESSAGE_TEXT]
        messages[key] = clean
    return messages


def _clean_optional_text(raw, label, maximum):
    if raw in (None, ""):
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"Plugin {label} must be text.")
    return raw.strip()[:maximum]


def _clean_homepage(raw):
    if raw in (None, ""):
        return ""
    if not isinstance(raw, str):
        raise ValueError("Plugin homepage must be a URL.")
    value = raw.strip()
    parts = urlsplit(value)
    if len(value) > 300 or parts.scheme != "https" or not parts.netloc:
        raise ValueError("Plugin homepage must be an HTTPS URL of at most 300 characters.")
    return value


def _clean_capability_identity(name, value):
    if not isinstance(value, dict):
        raise ValueError(f"Plugin capability {name} must be an object.")
    capability_id = value.get("id")
    capability_name = value.get("name")
    if not isinstance(capability_id, str) or not CAPABILITY_ID.fullmatch(capability_id):
        raise ValueError(f"Plugin capability {name} needs a valid id.")
    if not isinstance(capability_name, str) or not capability_name.strip():
        raise ValueError(f"Plugin capability {name} needs a name.")
    return capability_id, capability_name.strip()[:MAX_CAPABILITY_NAME]


def _clean_media_kinds(raw, name):
    if raw is None:
        return ["cover", "background"]
    if not isinstance(raw, list) or len(raw) > MAX_CAPABILITY_MEDIA_KINDS:
        raise ValueError(f"Plugin capability {name} media_kinds must be a bounded list.")
    allowed = _capability_media_kinds()
    kinds = []
    for kind in raw:
        if not isinstance(kind, str) or kind not in allowed:
            raise ValueError(f"Plugin capability {name} declares an unknown media kind.")
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _clean_emulator_capabilities(raw):
    if not isinstance(raw, list) or len(raw) > MAX_EMULATOR_CAPABILITIES:
        raise ValueError("Plugin emulator_settings must be a list of at most 8 emulators.")
    emulators = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Plugin emulator_settings entries must be objects.")
        emulator_id = entry.get("emulator_id")
        if not isinstance(emulator_id, str) or not EMULATOR_CAPABILITY_ID.fullmatch(emulator_id):
            raise ValueError("Plugin emulator_settings needs a valid emulator_id.")
        if emulator_id in seen:
            raise ValueError("Plugin emulator_settings ids must be unique.")
        seen.add(emulator_id)
        label = entry.get("label")
        if label in (None, ""):
            label = emulator_id
        if not isinstance(label, str):
            raise ValueError("Plugin emulator_settings label must be text.")
        schema = _clean_settings_schema(entry.get("schema"))
        if schema is None:
            raise ValueError("Plugin emulator_settings requires a settings schema.")
        scopes_raw = entry.get("scopes", ["global", "platform"])
        if not isinstance(scopes_raw, list) or not set(scopes_raw) <= {"global", "platform"}:
            raise ValueError("Plugin emulator_settings scopes must be global and/or platform.")
        scopes = [scope for scope in ("global", "platform") if scope in scopes_raw]
        if not scopes:
            raise ValueError("Plugin emulator_settings needs at least one scope.")
        emulators.append({
            "emulator_id": emulator_id,
            "label": label.strip()[:MAX_CAPABILITY_NAME],
            "schema": schema,
            "scopes": scopes,
        })
    return emulators


def _clean_capabilities(raw, plugin_id, hooks=None):
    """Validate the additive capability declarations.

    A capability is only accepted when the plugin also declares its hook, so
    a capability can never widen what the host is willing to invoke. When
    ``hooks`` is omitted every capability hook is treated as declared (used
    by callers that validate shape only).
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Plugin capabilities must be an object.")
    hooks = set(CAPABILITY_HOOK_NAMES if hooks is None else hooks)
    capabilities = {}
    seen_ids = set()
    for name, hook in CAPABILITY_HOOKS.items():
        if name not in raw:
            continue
        if hook not in hooks:
            raise ValueError(f"Plugin capability {name} requires the {hook} hook.")
        value = raw[name]
        if name == "emulator_settings":
            capabilities[name] = _clean_emulator_capabilities(value)
            continue
        capability_id, capability_name = _clean_capability_identity(name, value)
        if capability_id in seen_ids:
            raise ValueError(f"Plugin capability id {capability_id!r} is duplicated in {plugin_id}.")
        seen_ids.add(capability_id)
        capability = {"id": capability_id, "name": capability_name}
        if name in ("storefront", "metadata"):
            platform = value.get("platform", "")
            if platform not in (None, "") and not isinstance(platform, str):
                raise ValueError(f"Plugin {name} platform must be text.")
            capability["platform"] = str(platform or "").strip()[:MAX_CAPABILITY_PLATFORM]
        elif name == "media":
            capability["media_kinds"] = _clean_media_kinds(value.get("media_kinds"), name)
        capabilities[name] = capability
    return capabilities


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


def _package_files(root):
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _legacy_plugin_checksum(directory, plugin_id):
    """The digest grants recorded before the framing change used: raw contents, no file framing.

    Only to tell a user that an old grant must be approved again. It never grants trust,
    because it matches a package whose bytes were moved between files.
    """
    root = Path(directory) / str(plugin_id)
    digest = hashlib.sha256()
    for path in _package_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    return digest.hexdigest()


def plugin_checksum(directory, plugin_id):
    """sha256 over the installed package (relative paths + contents).

    Computed at trust-prompt time for manual installs; a mismatch against the
    recorded checksum re-prompts, so updates cannot inherit old trust.
    """
    root = Path(directory) / str(plugin_id)
    digest = hashlib.sha256()
    for path in _package_files(root):
        # Frame each file as path NUL + sha256(content). Hashing raw content
        # back to back left the content/next-path boundary ambiguous, so a
        # package could move bytes between a file's tail and the next file
        # name (e.g. into plugin.py) and keep the trusted checksum.
        file_digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                file_digest.update(chunk)
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(file_digest.digest())
    return digest.hexdigest()


def plugin_trust_status(directory, plugin_id):
    """Trust record for one plugin; trusted only when the checksum matches."""
    root = Path(directory)
    try:
        checksum = plugin_checksum(root, plugin_id)
    except OSError:
        return {"trusted": False, "checksum": "", "checksum_matches": False, "granted_at": "", "retrust": False}
    record = load_plugin_state(root).get("trust", {}).get(str(plugin_id))
    record = record if isinstance(record, dict) else {}
    checksum_matches = record.get("checksum") == checksum
    # A grant from before the framing change is asked for again, never inherited.
    retrust = False
    if record.get("trusted") and not checksum_matches:
        try:
            retrust = record.get("checksum") == _legacy_plugin_checksum(root, plugin_id)
        except OSError:
            retrust = False
    return {
        "trusted": bool(record.get("trusted") and checksum_matches),
        "checksum": checksum,
        "checksum_matches": bool(checksum_matches),
        "granted_at": str(record.get("granted_at") or ""),
        "retrust": bool(retrust),
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


def _secret_setting_names(manifest):
    schema = manifest.get("settings") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return set()
    return {
        str(name) for name, field in properties.items()
        if isinstance(field, dict) and field.get("format") == "password"
    }


def plugin_settings_values_public(directory, manifest):
    """Stored settings minus password-format values (safe for hook payloads)."""
    values = plugin_settings_values(directory, manifest)
    secrets = _secret_setting_names(manifest)
    return {name: value for name, value in values.items() if name not in secrets}


def get_plugin_settings_public(directory, plugin_id):
    """Schema plus redacted values for Settings > Plugins.

    Password fields are never returned; ``secret_set`` tells the form whether
    a value already exists without disclosing it.
    """
    root = Path(directory)
    manifest = read_manifest(root / str(plugin_id))
    secrets = _secret_setting_names(manifest)
    values = plugin_settings_values(directory, manifest)
    return {
        "schema": manifest.get("settings") or {"type": "object", "properties": {}},
        "values": {name: value for name, value in values.items() if name not in secrets},
        "secret_set": {name: bool(values.get(name)) for name in sorted(secrets)},
    }


def _plugin_store_path(directory, folder, plugin_id):
    plugin_id = str(plugin_id)
    if not PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError("Plugin id is invalid.")
    return Path(directory) / folder / f"{plugin_id}.json"


def _read_json_store(path, maximum):
    try:
        if path.stat().st_size > maximum:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_store(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def _json_depth(value):
    """Depth of a JSON value; raises ValueError for non-JSON data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("Plugin data must be finite JSON.")
        return 0
    if isinstance(value, list):
        return 1 + max((_json_depth(item) for item in value), default=0)
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > MAX_PLUGIN_DATA_KEY:
                raise ValueError("Plugin data keys must be short strings.")
            _json_depth(item)
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    raise ValueError("Plugin data must be JSON serializable.")


def plugin_data_values(directory, plugin_id):
    """The plugin's bounded data bag (``{}`` when absent or malformed)."""
    try:
        path = _plugin_store_path(directory, "data", plugin_id)
    except ValueError:
        return {}
    data = _read_json_store(path, MAX_PLUGIN_DATA)
    try:
        if data and _json_depth(data) > MAX_PLUGIN_DATA_DEPTH:
            return {}
    except ValueError:
        return {}
    return data


def set_plugin_data(directory, plugin_id, patch):
    """Shallow-merge a validated patch into the plugin data bag.

    The whole merged bag must stay under 1 MiB and four levels deep; a patch
    that breaks either bound raises ``ValueError`` and writes nothing.
    """
    if not isinstance(patch, dict):
        raise ValueError("Plugin data must be an object.")
    cleaned = {}
    for key, value in patch.items():
        if not isinstance(key, str) or not key or len(key) > MAX_PLUGIN_DATA_KEY:
            raise ValueError("Plugin data keys must be short strings.")
        _json_depth(value)
        cleaned[key] = value
    merged = {**plugin_data_values(directory, plugin_id), **cleaned}
    if _json_depth(merged) > MAX_PLUGIN_DATA_DEPTH:
        raise ValueError("Plugin data is nested too deeply.")
    encoded = json.dumps(merged, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_PLUGIN_DATA:
        raise ValueError("Plugin data exceeds the 1 MiB limit.")
    _write_json_store(_plugin_store_path(directory, "data", plugin_id), merged)
    return merged


def plugin_credentials_values(directory, plugin_id):
    """Credential values for a capability hook (plain object, 0600 file)."""
    try:
        path = _plugin_store_path(directory, "credentials", plugin_id)
    except ValueError:
        return {}
    raw = _read_json_store(path, MAX_PLUGIN_DATA)
    values = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or len(key) > MAX_PLUGIN_DATA_KEY:
            continue
        if isinstance(value, str) and len(value) <= MAX_CREDENTIAL_VALUE:
            values[key] = value
    return values


def set_plugin_credentials(directory, plugin_id, values, *, clear=None):
    """Merge credential values; an empty string or a clear entry deletes.

    Returns the stored key names (never the values).
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("Plugin credentials must be an object.")
    if clear is None:
        clear = []
    if not isinstance(clear, list) or any(not isinstance(item, str) for item in clear):
        raise ValueError("Plugin credential clear list must be a list of names.")
    existing = plugin_credentials_values(directory, plugin_id)
    for key, value in values.items():
        if not isinstance(key, str) or not key or len(key) > MAX_PLUGIN_DATA_KEY:
            raise ValueError("Plugin credential names must be short strings.")
        if value in ("", None):
            existing.pop(key, None)
            continue
        if not isinstance(value, str) or len(value) > MAX_CREDENTIAL_VALUE:
            raise ValueError("Plugin credential values must be strings of at most 4096 characters.")
        existing[key] = value
    for key in clear:
        existing.pop(key, None)
    if len(existing) > MAX_CREDENTIAL_KEYS:
        raise ValueError("Plugins may store at most 128 credentials.")
    path = _plugin_store_path(directory, "credentials", plugin_id)
    if existing:
        _write_json_store(path, existing)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return sorted(existing)


def plugin_credentials_summary(directory, plugin_id):
    """Key names and presence flags for the UI; values are never returned."""
    values = plugin_credentials_values(directory, plugin_id)
    return {"keys": sorted(values), "set": {key: True for key in sorted(values)}}


def _resolve_hook_timeout(hook, timeout):
    if timeout is not None:
        return timeout
    return HOOK_TIMEOUTS.get(hook, HOOK_TIMEOUTS["default"])


def _hook_payload(root, manifest, hook, payload):
    """Inject host data/credentials/settings into a hook payload."""
    if not isinstance(payload, dict):
        return payload
    body = dict(payload)
    body["plugin_data"] = plugin_data_values(root, manifest["id"])
    capability = hook in PRIVILEGED_HOOK_NAMES
    if manifest.get("settings"):
        values = plugin_settings_values_public(root, manifest) if capability else plugin_settings_values(root, manifest)
        if not capability:
            # Password-format settings are credentials; legacy hooks never see them.
            secrets = _secret_setting_names(manifest)
            values = {key: value for key, value in values.items() if key not in secrets}
        body["settings"] = values
    if capability:
        body["credentials"] = plugin_credentials_values(root, manifest["id"])
    return body


def resolve_plugin_notification(manifest, notification, *, locale="en"):
    """Resolve a hook notification to ``{level, message}``.

    Keys are looked up in the manifest message table for the active locale,
    then English, then the key itself. Formatting failures fall back to the
    raw template rather than dropping the notification.
    """
    if isinstance(notification, str):
        notification = {"message": notification}
    if not isinstance(notification, dict):
        raise ValueError("Plugin notification must be an object.")
    level = notification.get("level", "info")
    if level not in NOTIFICATION_LEVELS:
        level = "info"
    key = notification.get("key")
    params = notification.get("params")
    if not isinstance(params, dict):
        params = {}
    message = ""
    if key not in (None, ""):
        messages = manifest.get("messages") or {}
        translations = messages.get(str(key)) if isinstance(messages, dict) else None
        if isinstance(translations, dict):
            template = translations.get(locale) or translations.get("en")
        else:
            template = None
        if template is None:
            template = str(key)[:MAX_MESSAGE_TEXT]
        safe_params = {
            str(name)[:64]: str(value)[:200]
            for name, value in list(params.items())[:32]
        }
        try:
            message = template.format(**safe_params)
        except (KeyError, ValueError, IndexError):
            message = template
    if not message:
        raw_message = notification.get("message")
        message = raw_message if isinstance(raw_message, str) else ""
    return {"level": level, "message": str(message)[:2000]}


def _active_locale():
    try:
        from openbox import load_state_readonly
        state = load_state_readonly()
        locale = state.get("settings", {}).get("locale") if isinstance(state, dict) else None
        return str(locale or "en")[:5] or "en"
    except Exception:  # state is optional for notification localization
        return "en"


def _persist_plugin_notification(plugin_id, event, resolved):
    """Store one plugin notification through the shared notification store."""
    from notifications import add_notification
    from openbox import update_state

    def mutate(state):
        add_notification(
            state,
            kind="plugin",
            level=resolved["level"],
            title=resolved["message"][:200],
            body=resolved["message"],
            source=plugin_id,
            dedupe_key=f"plugin:{plugin_id}:{event}",
        )

    update_state(mutate)


def _service_state():
    try:
        from openbox import load_state_readonly
        state = load_state_readonly()
        if not isinstance(state, dict):
            return {}
        # Host services must see the same hidden/plugin-filtered games the UI
        # sees, not raw persisted rows. A capability hook can run while the
        # public payload itself is being built (discovery), so never recurse.
        try:
            from pkg.state.cache import BUILDING_PUBLIC_STATE, _filter_plugin_storefront_games
            if BUILDING_PUBLIC_STATE.get():
                from openbox import DATA
                public_games = _filter_plugin_storefront_games(state.get("games", []), Path(DATA).parent)
            else:
                from webapp_state import public_state
                public_games = public_state().get("games")
        except Exception:
            public_games = None
        if isinstance(public_games, list):
            state = dict(state)
            state["games"] = public_games
        return state
    except Exception:
        LOGGER.warning("Plugin service request could not read library state", exc_info=True)
        return {}


def _execute_service_requests(requests):
    """Run at most eight allowlisted read-only host services."""
    from pkg.parity.parity_plugin_services import handle_service

    if not isinstance(requests, list) or len(requests) > 8:
        raise ValueError("service_requests must be a list of at most 8 requests.")
    state = _service_state()
    results = {}
    for request in requests:
        if not isinstance(request, dict):
            raise ValueError("Every service request must be an object.")
        request_id = request.get("id")
        action = request.get("action")
        params = request.get("params", {})
        if not isinstance(request_id, str) or not request_id or len(request_id) > 64:
            raise ValueError("Service requests need a short id.")
        if request_id in results:
            raise ValueError("Service request ids must be unique.")
        if not isinstance(action, str) or not action:
            raise ValueError("Service requests need an action.")
        if not isinstance(params, dict):
            raise ValueError("Service request params must be an object.")
        try:
            results[request_id] = handle_service(action, params, state)
        except Exception as error:
            # A failing optional service is reported for that request; the
            # hook still gets its single second invocation.
            LOGGER.warning("Plugin service %s failed: %s", action, error)
            results[request_id] = {"error": str(error)[:500] or "Service failed."}
    return results


def _finalize_hook_result(root, manifest, candidate):
    """Merge plugin data and strip forbidden result channels."""
    if "credentials" in candidate:
        candidate.pop("credentials", None)
        LOGGER.warning(
            "Plugin %s returned credentials; hooks cannot write the credential store",
            manifest["id"],
        )
    if "data" in candidate:
        data = candidate.get("data")
        if isinstance(data, dict):
            try:
                set_plugin_data(root, manifest["id"], data)
            except (ValueError, OSError) as error:
                LOGGER.warning("Plugin %s data was rejected: %s", manifest["id"], error)
        else:
            LOGGER.warning("Plugin %s returned non-object data; ignored", manifest["id"])
    return candidate


def emit_plugin_event(directory, event, payload=None):
    """Best-effort fan-out of a lifecycle event to ``events``-hook plugins.

    Never raises: a failing event path must not break the operation that
    emitted it (imports, session bookkeeping, shutdown). Notifications
    returned by event hooks are resolved through the manifest message table
    and stored in the shared notification center.
    """
    if os.environ.get("OPENBOX_SAFE_MODE"):
        return
    try:
        body = {"event": event}
        if isinstance(payload, dict):
            body.update(payload)
        results = run_plugin_hooks(Path(directory), "events", body)
        if not results:
            return
        root = Path(directory)
        locale = _active_locale()
        manifests = {}
        for plugin_id, candidate, error in results:
            if error:
                LOGGER.warning("Plugin %s failed for event %s: %s", plugin_id, event, error)
                continue
            if not isinstance(candidate, dict):
                continue
            notification = candidate.get("notification")
            if not isinstance(notification, dict):
                continue
            manifest = manifests.get(plugin_id)
            if manifest is None:
                try:
                    manifest = read_manifest(root / plugin_id)
                except ValueError:
                    continue
                manifests[plugin_id] = manifest
            try:
                resolved = resolve_plugin_notification(manifest, notification, locale=locale)
            except ValueError as resolution_error:
                LOGGER.warning("Plugin %s notification ignored: %s", plugin_id, resolution_error)
                continue
            if not resolved["message"]:
                continue
            try:
                _persist_plugin_notification(plugin_id, event, resolved)
            except Exception as store_error:
                LOGGER.warning("Plugin %s notification could not be stored: %s", plugin_id, store_error)
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
    messages = _clean_messages(manifest.get("messages"))
    capabilities = _clean_capabilities(manifest.get("capabilities"), plugin_id, hooks)
    return {
        **manifest,
        "id": plugin_id,
        "entry": entry,
        "hooks": hooks,
        "api_version": api_version,
        "commands": commands,
        "permissions": permissions,
        "settings": settings,
        "capabilities": capabilities,
        "messages": messages,
        "description": _clean_optional_text(manifest.get("description"), "description", 500),
        "author": _clean_optional_text(manifest.get("author"), "author", 80),
        "homepage": _clean_homepage(manifest.get("homepage")),
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
            "retrust": trust["retrust"],
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


def run_plugin_hook(directory, plugin_id, hook, payload, *, timeout=None):
    """Run one plugin hook and return ``(result, error)``.

    ``error`` is a human-readable reason when the plugin was skipped (invalid
    plugin id, unsandboxed host, oversized payload, missing runner). The result
    is the parsed dict output or None.

    ``timeout`` defaults to ``HOOK_TIMEOUTS`` for the hook (5 s for legacy
    hooks, longer for capability hooks).
    """
    root = Path(directory)
    try:
        manifest = read_manifest(root / str(plugin_id))
    except ValueError as error:
        return None, str(error)
    if hook not in manifest["hooks"]:
        return None, f"Plugin does not declare the {hook} hook."
    return _run_manifest_hook(root, manifest, hook, payload, timeout=timeout)


def _run_manifest_hook(root, manifest, hook, payload, *, timeout=None):
    """Invoke a hook, then run at most one service round-trip.

    Capability hooks may return ``service_requests``; the host executes them
    and re-invokes the same hook once with ``service_results``. Data and
    notifications from the first result are never merged.
    """
    candidate, error = _invoke_hook(root, manifest, hook, payload, timeout=timeout)
    if error or not isinstance(candidate, dict):
        return candidate, error
    if "service_requests" in candidate:
        if hook not in PRIVILEGED_HOOK_NAMES:
            return None, "service_requests are only available to capability hooks."
        try:
            results = _execute_service_requests(candidate.get("service_requests"))
        except ValueError as service_error:
            return None, str(service_error)
        forwarded = dict(payload) if isinstance(payload, dict) else {}
        forwarded["service_results"] = results
        candidate, error = _invoke_hook(root, manifest, hook, forwarded, timeout=timeout)
        if error or not isinstance(candidate, dict):
            return candidate, error
        if "service_requests" in candidate:
            candidate.pop("service_requests", None)
            LOGGER.warning(
                "Plugin %s requested services more than once; the second round was ignored",
                manifest["id"],
            )
    return _finalize_hook_result(root, manifest, candidate), ""


def _invoke_hook(root, manifest, hook, payload, *, timeout=None):
    package_root = root / manifest["id"]
    entry = package_root / manifest["entry"]
    # Per-plugin settings travel in the stdin JSON payload — the sanctioned
    # channel that sidesteps the environment scrubbing (F1c). Capability
    # hooks additionally receive the plugin data bag and credentials.
    payload = _hook_payload(root, manifest, hook, payload)
    injected_data = payload.get("plugin_data") if isinstance(payload, dict) else None
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
                timeout=_resolve_hook_timeout(hook, timeout), env=plugin_env, start_new_session=True,
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
    # Legacy plugins often echo their input payload. The host-added data bag
    # is new; drop it when it comes back unchanged so v1 result shapes stay
    # exactly what they were (capability hooks keep it).
    if (
        hook not in PRIVILEGED_HOOK_NAMES
        and "plugin_data" in candidate
        and candidate.get("plugin_data") == injected_data
    ):
        candidate.pop("plugin_data", None)
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
    for folder in ("data", "credentials"):
        try:
            _plugin_store_path(root, folder, manifest["id"]).unlink()
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            LOGGER.warning("Plugin %s %s store could not be removed: %s", manifest["id"], folder, error)
    try:
        (root.parent / "plugin-stats" / f"{manifest['id']}.json").unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        LOGGER.warning("Plugin %s stats cache could not be removed: %s", manifest["id"], error)
    return manifest["id"]


def run_plugin_hooks(directory, hook, payload, *, timeout=None):
    """Run one hook across enabled plugins, keeping per-plugin attribution.

    Returns ``[(plugin_id, candidate, error), ...]`` in manifest order. Unlike
    :func:`run_plugins` the results are not chained; callers decide what to
    merge.
    """
    root = Path(directory)
    results = []
    for manifest in list_plugins(root):
        if not manifest.get("valid") or not manifest["enabled"] or hook not in manifest["hooks"]:
            continue
        candidate, error = _run_manifest_hook(root, manifest, hook, payload, timeout=timeout)
        results.append((manifest["id"], candidate, error))
    return results


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
