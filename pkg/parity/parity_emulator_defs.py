"""YAML emulator registry, launch resolution, and ROM scan helpers."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pkg.parity.launch_tokens import apply_tokens, build_launch_args  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

SCHEMA_VERSION = 1
_REGISTRY_CACHE: dict | None = None


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "emulator_defs").is_dir():
        return candidate
    for parent in candidate.parents:
        if (parent / "emulator_defs").is_dir():
            return parent
    return candidate


ROOT = _repo_root()
DEFS_DIR = ROOT / "emulator_defs"


def _parse_yaml(text):
    if yaml is not None:
        return yaml.safe_load(text)
    data = {}
    current = None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            if line.endswith(":"):
                current = line[:-1].strip()
                data[current] = None
            elif ":" in line:
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip().strip('"').strip("'")
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            item = stripped[2:].strip().strip('"').strip("'")
        else:
            item = stripped.strip('"').strip("'")
        if not item:
            continue
        if data[current] is None:
            data[current] = [item]
        elif isinstance(data[current], list):
            data[current].append(item)
        else:
            data[current] = [data[current], item]
    return data


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compile_startup_args(raw):
    if isinstance(raw.get("startup_args"), list):
        return [str(item) for item in raw["startup_args"]]
    startup = str(raw.get("startup", "") or "").strip()
    if not startup:
        return ["{path}"]
    try:
        return shlex.split(startup)
    except ValueError:
        return startup.split()


def _normalize_adapter(raw):
    if not isinstance(raw, dict):
        raise ValueError("Adapter definition must be a mapping.")
    adapter_id = str(raw.get("adapter_id") or raw.get("id") or "").strip()
    emulator_id = str(raw.get("emulator_id") or raw.get("flatpak") or "").strip()
    platform = str(raw.get("platform") or "").strip()
    if not adapter_id or not emulator_id or not platform:
        raise ValueError("Adapter requires adapter_id, emulator_id, and platform.")
    extensions = raw.get("extensions") or []
    if isinstance(extensions, str):
        extensions = [extensions]
    native_exe = raw.get("native_exe", raw.get("native"))
    flatpak_app_id = raw.get("flatpak_app_id", raw.get("flatpak"))
    return {
        "adapter_id": adapter_id,
        "emulator_id": emulator_id,
        "label": str(raw.get("label") or raw.get("name") or adapter_id),
        "platform": platform,
        "extensions": [ext.lower().lstrip(".") for ext in extensions],
        "native_exe": str(native_exe).strip() if native_exe else None,
        "flatpak_app_id": str(flatpak_app_id).strip() if flatpak_app_id else None,
        "startup_args": _compile_startup_args(raw),
        "recommended": _as_bool(raw.get("recommended"), default=True),
        "priority": _as_int(raw.get("priority"), default=100),
        "executable_patterns": [str(item) for item in (raw.get("executable_patterns") or [])],
        "schema_version": _as_int(raw.get("schema_version"), default=SCHEMA_VERSION),
    }


def _load_raw_adapters(defs_dir=None):
    folder = Path(defs_dir or DEFS_DIR)
    adapters = []
    if not folder.is_dir():
        return adapters
    for path in sorted(folder.glob("*.yaml")):
        try:
            payload = _parse_yaml(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            adapters.append(_normalize_adapter(payload))
        except ValueError:
            continue
    return adapters


def load_adapters(defs_dir=None):
    return list(_load_raw_adapters(defs_dir))


def load_registry(defs_dir=None):
    adapters = load_adapters(defs_dir)
    schema_version = adapters[0]["schema_version"] if adapters else SCHEMA_VERSION
    return {
        "schema_version": schema_version,
        "adapters": [
            {
                "adapter_id": item["adapter_id"],
                "emulator_id": item["emulator_id"],
                "label": item["label"],
                "platform": item["platform"],
                "extensions": list(item["extensions"]),
                "native_exe": item["native_exe"],
                "flatpak_app_id": item["flatpak_app_id"],
                "startup_args": list(item["startup_args"]),
                "recommended": item["recommended"],
                "priority": item["priority"],
            }
            for item in adapters
        ],
    }


def _registry():
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = {
            "adapters": load_adapters(),
            "by_adapter_id": {},
            "by_emulator_id": {},
            "by_platform": {},
            "by_extension": {},
        }
        for adapter in _REGISTRY_CACHE["adapters"]:
            _REGISTRY_CACHE["by_adapter_id"][adapter["adapter_id"]] = adapter
            _REGISTRY_CACHE["by_emulator_id"].setdefault(adapter["emulator_id"], []).append(adapter)
            _REGISTRY_CACHE["by_platform"].setdefault(adapter["platform"], []).append(adapter)
            for extension in adapter["extensions"]:
                _REGISTRY_CACHE["by_extension"].setdefault(extension, []).append(adapter)
        for key in ("by_emulator_id", "by_platform", "by_extension"):
            for value in _REGISTRY_CACHE[key].values():
                value.sort(key=lambda item: (not item["recommended"], item["priority"], item["adapter_id"]))
    return _REGISTRY_CACHE


def _reset_registry_cache():
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None


def load_definitions(defs_dir=None):
    if defs_dir is not None:
        adapters = load_adapters(defs_dir)
    else:
        adapters = _registry()["adapters"]
    definitions = []
    for adapter in adapters:
        definitions.append({
            "id": adapter["adapter_id"],
            "name": adapter["label"],
            "extensions": list(adapter["extensions"]),
            "platforms": [adapter["platform"]],
            "startup": shlex.join(adapter["startup_args"]),
            "startup_args": list(adapter["startup_args"]),
            "executable_patterns": list(adapter["executable_patterns"]),
            "flatpak": adapter["flatpak_app_id"] or "",
            "native": adapter["native_exe"] or "",
            "emulator_id": adapter["emulator_id"],
            "emulator_def": adapter["adapter_id"],
        })
    return definitions


def build_emulators_dict(adapters=None):
    adapters = adapters or _registry()["adapters"]
    emulators = {}
    for adapter in adapters:
        app_id = adapter["emulator_id"]
        entry = emulators.setdefault(app_id, {
            "name": adapter["label"].split(" (")[0],
            "native": adapter["native_exe"] or "",
            "profiles": {},
        })
        if not entry["native"] and adapter["native_exe"]:
            entry["native"] = adapter["native_exe"]
        entry["profiles"][adapter["platform"]] = shlex.join(adapter["startup_args"])
    return emulators


def build_platform_emulators(adapters=None):
    adapters = adapters or _registry()["adapters"]
    grouped = {}
    for adapter in adapters:
        grouped.setdefault(adapter["platform"], []).append(
            (adapter["emulator_id"], adapter["label"].split(" (")[0])
        )
    result = {}
    for platform, items in grouped.items():
        seen = set()
        ordered = []
        for app_id, name in items:
            if app_id in seen:
                continue
            seen.add(app_id)
            ordered.append((app_id, name))
        result[platform] = ordered
    return result


def build_platform_by_extension(adapters=None):
    adapters = adapters or _registry()["adapters"]
    mapping = {}
    for adapter in adapters:
        for extension in adapter["extensions"]:
            dotted = f".{extension}"
            if dotted not in mapping:
                mapping[dotted] = adapter["platform"]
    return mapping


EMULATORS = build_emulators_dict()
PLATFORM_EMULATORS = build_platform_emulators()


def find_adapter(adapter_id="", emulator_id=""):
    registry = _registry()
    adapter_id = str(adapter_id or "").strip()
    emulator_id = str(emulator_id or "").strip()
    if adapter_id:
        found = registry["by_adapter_id"].get(adapter_id)
        if found:
            return found
    if emulator_id:
        matches = registry["by_emulator_id"].get(emulator_id, [])
        if len(matches) == 1:
            return matches[0]
    return None


def detect_adapter_prefix(adapter, which=None):
    which = which or shutil.which
    native = adapter.get("native_exe") or ""
    if native and which(native):
        return [which(native)]
    flatpak = adapter.get("flatpak_app_id") or ""
    if flatpak and which("flatpak"):
        return ["flatpak", "run", flatpak]
    for pattern in adapter.get("executable_patterns", []):
        found = which(pattern)
        if found:
            return [found]
    return []


def detect_emulator(definition):
    return detect_adapter_prefix(definition)


def detect_adapter_for_platform(platform, which=None):
    which = which or shutil.which
    for adapter in _registry()["by_platform"].get(platform, []):
        if detect_adapter_prefix(adapter, which=which):
            return adapter
    return None


def build_adapter_argv(adapter, game, rom_path, prefix=None, data_dir="", which=None):
    which = which or shutil.which
    prefix = prefix or detect_adapter_prefix(adapter, which=which)
    if not prefix:
        raise FileNotFoundError(f"No emulator found for {adapter.get('label', adapter.get('adapter_id'))}.")
    args = list(prefix)
    emu_dir = str(Path(prefix[0]).parent) if prefix else ""
    for value in adapter.get("startup_args", []):
        args.append(
            apply_tokens(str(value), game, path=str(rom_path), emulator_dir=emu_dir, data_dir=data_dir)
        )
    return args


def build_launch_command(definition, rom_path, prefix=None):
    prefix = prefix or detect_emulator(definition)
    if not prefix:
        raise FileNotFoundError(f"No emulator found for {definition.get('name', definition.get('id'))}.")
    adapter = {
        "adapter_id": definition.get("id", ""),
        "label": definition.get("name", definition.get("id", "")),
        "startup_args": definition.get("startup_args") or _compile_startup_args(definition),
    }
    return build_adapter_argv(adapter, definition, rom_path, prefix=prefix)


def candidates_for_extension(extension, definitions=None):
    extension = str(extension).lower().lstrip(".")
    if definitions is not None:
        matches = []
        for definition in definitions:
            if extension in definition.get("extensions", []):
                matches.append(definition)
        return matches
    return list(_registry()["by_extension"].get(extension, []))


def platform_for_extension(extension, definitions=None):
    matches = candidates_for_extension(extension, definitions=definitions)
    if not matches:
        return "", None
    first = matches[0]
    if definitions is not None:
        platform = first.get("platforms", ["ROM"])[0]
        return platform, first
    return first.get("platform", ""), {
        "id": first["adapter_id"],
        "name": first["label"],
        "extensions": list(first["extensions"]),
        "platforms": [first["platform"]],
        "startup": shlex.join(first["startup_args"]),
        "startup_args": list(first["startup_args"]),
        "executable_patterns": list(first["executable_patterns"]),
        "flatpak": first["flatpak_app_id"] or "",
        "native": first["native_exe"] or "",
        "emulator_id": first["emulator_id"],
        "emulator_def": first["adapter_id"],
    }


def resolve_launch(game, profiles, *, which=None, data_dir=""):
    which = which or shutil.which
    path = str(game.get("path", "") or "")
    if not path:
        raise ValueError(f"{game.get('name', 'This game')} has no launch path.")
    if not Path(path).exists():
        raise FileNotFoundError(f"The configured path no longer exists:\n{path}")
    launch_path = path
    game_command = str(game.get("launch", "") or "").strip()
    platform = str(game.get("platform", "") or "")
    precedence = "direct_exe"
    args = None

    if game_command:
        args = build_launch_args(game_command, game, path=launch_path, data_dir=data_dir)
        if "{path}" not in game_command and "{ImagePath}" not in game_command:
            args.append(launch_path)
        precedence = "game_launch"
    else:
        adapter = find_adapter(game.get("emulator_adapter_id", ""), game.get("emulator_id", ""))
        if adapter:
            try:
                args = build_adapter_argv(adapter, game, launch_path, data_dir=data_dir, which=which)
                precedence = "game_adapter"
            except FileNotFoundError:
                args = None
        if args is None:
            profile_command = str(profiles.get(platform, "") or "").strip()
            if profile_command:
                args = build_launch_args(profile_command, game, path=launch_path, data_dir=data_dir)
                if "{path}" not in profile_command and "{ImagePath}" not in profile_command:
                    args.append(launch_path)
                precedence = "platform_profile"
        if args is None:
            detected = detect_adapter_for_platform(platform, which=which)
            if detected:
                try:
                    args = build_adapter_argv(detected, game, launch_path, data_dir=data_dir, which=which)
                    precedence = "registry_adapter"
                except FileNotFoundError:
                    args = None
    if args is None:
        if Path(launch_path).suffix.lower() == ".sh":
            args = ["bash", launch_path]
        else:
            args = [launch_path]
        precedence = "direct_exe"

    return {
        "args": args,
        "cwd": str(Path(launch_path).parent),
        "precedence": precedence,
    }


def scan_folder(folder, definitions=None, emulator_id=None, recursive=True):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError("Scan folder does not exist.")
    if definitions is not None:
        adapters = []
        for definition in definitions:
            adapters.append({
                "adapter_id": definition.get("id", ""),
                "emulator_id": definition.get("emulator_id") or definition.get("flatpak") or definition.get("id", ""),
                "label": definition.get("name", definition.get("id", "")),
                "platform": (definition.get("platforms") or ["ROM"])[0],
                "extensions": list(definition.get("extensions", [])),
            })
    else:
        adapters = list(_registry()["adapters"])
    emulator_id = str(emulator_id or "").strip()
    if emulator_id:
        adapters = [
            item for item in adapters
            if item.get("emulator_id") == emulator_id or item.get("adapter_id") == emulator_id
        ]
    extension_map = {}
    for adapter in adapters:
        for extension in adapter.get("extensions", []):
            extension_map.setdefault(extension, adapter)
    games = []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    for path in iterator:
        if not path.is_file():
            continue
        extension = path.suffix.lower().lstrip(".")
        adapter = extension_map.get(extension)
        if not adapter:
            continue
        platform = adapter.get("platform") or "ROM"
        games.append({
            "name": path.stem,
            "platform": platform,
            "path": str(path),
            "rom_name": path.name,
            "source": adapter.get("label", adapter.get("adapter_id", "Emulator")),
            "emulator_def": adapter.get("adapter_id"),
            "emulator_id": adapter.get("emulator_id"),
        })
    return games


def merge_profiles_from_definitions(existing_profiles, defs_dir=None):
    profiles = dict(existing_profiles or {})
    if defs_dir is not None:
        _reset_registry_cache()
        adapters = load_adapters(defs_dir)
    else:
        adapters = _registry()["adapters"]
    for adapter in adapters:
        prefix = detect_adapter_prefix(adapter)
        if not prefix:
            continue
        command = build_adapter_argv(adapter, {"name": adapter["label"]}, "{path}", prefix=prefix)
        profiles.setdefault(adapter["platform"], shlex.join(command))
    return profiles


def list_scan_configs(state):
    configs = state.get("settings", {}).get("emulator_scan_configs", [])
    return configs if isinstance(configs, list) else []


def save_scan_config(state, folder, emulator_id, auto_update=False):
    folder = str(folder).strip()
    emulator_id = str(emulator_id).strip()
    if not folder or not emulator_id:
        raise ValueError("Folder and emulator id are required.")
    configs = list_scan_configs(state)
    entry = {"folder": folder, "emulator_id": emulator_id, "auto_update": bool(auto_update)}
    configs = [item for item in configs if item.get("folder") != folder]
    configs.append(entry)
    state.setdefault("settings", {})["emulator_scan_configs"] = configs
    return entry


def main():
    text = """schema_version: 1
adapter_id: demo
emulator_id: demo.emulator
label: Demo
extensions:
  - iso
  - cso
platform: Single
startup_args:
  - "{path}"
recommended: true
priority: 1
"""
    data = _parse_yaml(text)
    assert data["adapter_id"] == "demo"
    assert data["label"] == "Demo"
    assert data["extensions"] == ["iso", "cso"]
    assert data["platform"] == "Single"
    print("emulator-defs fallback parser self-test: ok")


if __name__ == "__main__":
    main()
