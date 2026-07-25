"""Granular library backups with rotation."""

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path


BACKUP_ITEMS = {
    "settings": "settings.json",
    "library": "library.json",
    "media": "media",
    "plugins": "plugins",
    "themes": "themes",
    "extension_data": "extension-data",
}


def games_running(running_map):
    return bool(running_map)


def settings_snapshot(state):
    return state.get("settings", {})


def write_settings_file(data_dir, settings):
    path = Path(data_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2))


def read_settings_file(data_dir):
    path = Path(data_dir) / "settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_items(items):
    if not items:
        return ["library", "settings"]
    if isinstance(items, str):
        items = [part.strip() for part in items.split(",")]
    clean = []
    for item in items:
        key = str(item).strip().casefold()
        if key in BACKUP_ITEMS and key not in clean:
            clean.append(key)
    if not clean:
        raise ValueError("Select at least one backup item.")
    return clean


def backup_path(data_dir, prefix="OpenBoxBackup"):
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return Path(data_dir) / "backups" / f"{prefix}-{stamp}.zip"


def create_backup(data_dir, state, items, keep=0, running_map=None):
    if running_map is not None and games_running(running_map):
        raise ValueError("Close running games before creating a backup.")
    selected = normalize_items(items)
    root = Path(data_dir)
    archive = backup_path(root)
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"items": selected, "created": datetime.now().isoformat(timespec="seconds")}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("manifest.json", json.dumps(manifest, indent=2))
        if "settings" in selected:
            package.writestr("settings.json", json.dumps(settings_snapshot(state), indent=2))
        if "library" in selected:
            package.writestr("library.json", json.dumps(state, indent=2))
        for key in ("media", "plugins", "themes", "extension_data"):
            if key not in selected:
                continue
            folder = root / BACKUP_ITEMS[key]
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if path.is_file():
                    package.write(path, f"{BACKUP_ITEMS[key]}/{path.relative_to(folder).as_posix()}")
    if keep and keep > 0:
        rotate_backups(root / "backups", keep)
    return archive


def rotate_backups(folder, keep):
    if keep < 1:
        raise ValueError("Backup retention must be at least 1.")
    archives = sorted(Path(folder).glob("OpenBoxBackup-*.zip"), key=lambda path: path.stat().st_mtime)
    for path in archives[:-keep]:
        path.unlink(missing_ok=True)


def restore_backup(archive_path, data_dir, items=None, running_map=None):
    if running_map is not None and games_running(running_map):
        raise ValueError("Close running games before restoring a backup.")
    selected = normalize_items(items) if items else None
    root = Path(data_dir)
    try:
        with zipfile.ZipFile(archive_path) as package:
            manifest = {}
            if "manifest.json" in package.namelist():
                try:
                    manifest = json.loads(package.read("manifest.json"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("Backup manifest is invalid.") from error
                if not isinstance(manifest, dict):
                    raise ValueError("Backup manifest is invalid.")
            restore_items = selected or manifest.get("items") or ["library", "settings"]
            restore_items = normalize_items(restore_items)
            if "library" in restore_items and "library.json" in package.namelist():
                library_file = root / "library.json"
                if library_file.is_file():
                    shutil.copy2(library_file, library_file.with_name("library.before-restore.json"))
                library_file.write_bytes(package.read("library.json"))
            if "settings" in restore_items and "settings.json" in package.namelist():
                (root / "settings.json").write_bytes(package.read("settings.json"))
            for key in ("media", "plugins", "themes", "extension_data"):
                if key not in restore_items:
                    continue
                prefix = f"{BACKUP_ITEMS[key]}/"
                members = [name for name in package.namelist() if name.startswith(prefix) and not name.endswith("/")]
                target_root = (root / BACKUP_ITEMS[key]).resolve()
                target_root.mkdir(parents=True, exist_ok=True)
                for member in members:
                    relative = member[len(prefix) :]
                    target = (target_root / relative).resolve()
                    try:
                        target.relative_to(target_root)
                    except ValueError as error:
                        raise ValueError("Backup contains an unsafe path.") from error
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(package.read(member))
    except zipfile.BadZipFile as error:
        raise ValueError("Backup archive is invalid.") from error
    return restore_items
