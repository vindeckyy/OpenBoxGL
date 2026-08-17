"""YAML emulator definition packs and ROM scan helpers."""

import shlex
import shutil
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

def _repo_root() -> Path:
    # Handle both flat (parity_emulator_defs.py at root) and packaged (pkg/parity/) layouts
    candidate = Path(__file__).resolve().parent
    # If emulator_defs is at candidate, use it; otherwise climb toward repo root
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
        # A plain value becomes a one-element sequence, not a scalar: the
        # key's data type must come from the YAML shape, never its name.
        if data[current] is None:
            data[current] = [item]
        elif isinstance(data[current], list):
            data[current].append(item)
        else:
            data[current] = [data[current], item]
    return data


def main():
    # pyyaml absent: the fallback parser must not infer types from key names.
    text = """id: demo
name: Demo
extensions:
  - iso
  - cso
platform: Single
"""
    data = _parse_yaml(text)
    assert data["id"] == "demo"
    assert data["name"] == "Demo"
    assert data["extensions"] == ["iso", "cso"]
    assert data["platform"] == "Single"
    print("emulator-defs fallback parser self-test: ok")


if __name__ == "__main__":
    main()


def load_definitions(defs_dir=None):
    folder = Path(defs_dir or DEFS_DIR)
    definitions = []
    if not folder.is_dir():
        return definitions
    for path in sorted(folder.glob("*.yaml")):
        try:
            payload = _parse_yaml(path.read_text())
        except OSError:
            continue
        if not isinstance(payload, dict) or not payload.get("id"):
            continue
        payload["id"] = str(payload["id"])
        payload["name"] = str(payload.get("name", payload["id"]))
        payload["extensions"] = [ext.lower().lstrip(".") for ext in payload.get("extensions", [])]
        payload["platforms"] = list(payload.get("platforms", []))
        payload["startup"] = str(payload.get("startup", "{path}"))
        payload["executable_patterns"] = list(payload.get("executable_patterns", []))
        payload["flatpak"] = str(payload.get("flatpak", "")).strip()
        payload["native"] = str(payload.get("native", "")).strip()
        definitions.append(payload)
    return definitions


def detect_emulator(definition):
    native = definition.get("native", "")
    if native and shutil.which(native):
        return [native]
    flatpak = definition.get("flatpak", "")
    if flatpak and shutil.which("flatpak"):
        return ["flatpak", "run", flatpak]
    for pattern in definition.get("executable_patterns", []):
        if shutil.which(pattern):
            return [shutil.which(pattern)]
    return []


def platform_for_extension(extension, definitions=None):
    extension = str(extension).lower().lstrip(".")
    for definition in definitions or load_definitions():
        if extension in definition.get("extensions", []):
            platforms = definition.get("platforms", [])
            if platforms:
                return platforms[0], definition
    return "", None


def build_launch_command(definition, rom_path, prefix=None):
    prefix = prefix or detect_emulator(definition)
    if not prefix:
        raise FileNotFoundError(f"No emulator found for {definition.get('name', definition.get('id'))}.")
    args = list(prefix)
    startup = definition.get("startup", "{path}")
    try:
        template_args = shlex.split(str(startup))
    except ValueError as error:
        raise ValueError(f"Invalid startup command for {definition.get('name', definition.get('id'))}.") from error
    replacements = {"{path}": str(rom_path), "{name}": Path(rom_path).stem}
    args.extend(
        next(
            (value.replace(marker, replacement) for marker, replacement in replacements.items() if marker in value),
            value,
        )
        for value in template_args
    )
    return args


def scan_folder(folder, definitions=None, recursive=True):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError("Scan folder does not exist.")
    definitions = definitions or load_definitions()
    extension_map = {}
    for definition in definitions:
        for extension in definition.get("extensions", []):
            extension_map.setdefault(extension, definition)
    games = []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    for path in iterator:
        if not path.is_file():
            continue
        extension = path.suffix.lower().lstrip(".")
        definition = extension_map.get(extension)
        if not definition:
            continue
        platform = definition.get("platforms", ["ROM"])[0]
        games.append({
            "name": path.stem,
            "platform": platform,
            "path": str(path),
            "rom_name": path.name,
            "source": definition.get("name", definition.get("id", "Emulator")),
            "emulator_def": definition.get("id"),
        })
    return games


def merge_profiles_from_definitions(existing_profiles):
    profiles = dict(existing_profiles or {})
    for definition in load_definitions():
        prefix = detect_emulator(definition)
        if not prefix:
            continue
        command = build_launch_command(definition, "{path}", prefix=prefix)
        for platform in definition.get("platforms", []):
            profiles.setdefault(platform, " ".join(command))
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
