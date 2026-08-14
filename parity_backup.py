"""Granular library backups with rotation."""

import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from backend_io import atomic_copy_stream, atomic_write_text, fsync_directory
from state_store import default_state


BACKUP_ITEMS = {
    "settings": "settings.json",
    "library": "library.json",
    "media": "media",
    "plugins": "plugins",
    "themes": "themes",
    "extension_data": "extension-data",
}
MAX_BACKUP_MEMBERS = 50_000
MAX_BACKUP_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_BACKUP_TOTAL_BYTES = 32 * 1024 * 1024 * 1024


def games_running(running_map):
    return bool(running_map)


def settings_snapshot(state):
    return state.get("settings", {})


def write_settings_file(data_dir, settings):
    path = Path(data_dir) / "settings.json"
    atomic_write_text(path, json.dumps(settings, indent=2) + "\n")


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
    temporary = archive.with_name(f".{archive.name}.tmp")
    manifest = {"items": selected, "created": datetime.now().isoformat(timespec="seconds")}
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as package:
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
                if folder.is_symlink():
                    raise ValueError(f"Backup source is a symlink: {folder}")
                for path in folder.rglob("*"):
                    if path.is_symlink():
                        raise ValueError(f"Backup source contains a symlink: {path}")
                    if path.is_file():
                        package.write(path, f"{BACKUP_ITEMS[key]}/{path.relative_to(folder).as_posix()}")
        os.replace(temporary, archive)
        fsync_directory(archive.parent)
    finally:
        temporary.unlink(missing_ok=True)
    if keep and keep > 0:
        rotate_backups(root / "backups", keep)
    return archive


def rotate_backups(folder, keep):
    if keep < 1:
        raise ValueError("Backup retention must be at least 1.")
    archives = sorted(Path(folder).glob("OpenBoxBackup-*.zip"), key=lambda path: path.stat().st_mtime)
    for path in archives[:-keep]:
        path.unlink(missing_ok=True)


def restore_backup(archive_path, data_dir, items=None, running_map=None, force=False):
    if running_map is not None and games_running(running_map):
        raise ValueError("Close running games before restoring a backup.")
    selected = normalize_items(items) if items else None
    root = Path(data_dir)
    if root.is_symlink():
        raise ValueError("OpenBox data directory may not be a symlink.")
    try:
        with zipfile.ZipFile(archive_path) as package:
            infos = package.infolist()
            if len(infos) > MAX_BACKUP_MEMBERS:
                raise ValueError("Backup contains too many files.")
            total_bytes = 0
            seen_names = set()
            for info in infos:
                name = info.filename.replace("\\", "/")
                relative = Path(name)
                if not name or "\x00" in name or relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Backup contains an unsafe path.")
                normalized = "/".join(part for part in relative.parts if part not in {"."})
                if normalized in seen_names:
                    raise ValueError("Backup contains duplicate entries.")
                seen_names.add(normalized)
                mode = (info.external_attr >> 16) & 0o170000
                if mode in {0o120000, 0o060000}:
                    raise ValueError("Backup links are not supported.")
                if info.file_size > MAX_BACKUP_MEMBER_BYTES:
                    raise ValueError("Backup member is too large.")
                total_bytes += info.file_size
                if total_bytes > MAX_BACKUP_TOTAL_BYTES:
                    raise ValueError("Backup expands beyond the allowed size.")
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
            created = str(manifest.get("created", "")).strip()
            if not force and created and "library" in restore_items and "library.json" in package.namelist():
                current = root / "library.json"
                if current.is_file():
                    try:
                        current_time = datetime.fromisoformat(
                            datetime.fromtimestamp(current.stat().st_mtime).isoformat(timespec="seconds")
                        )
                        backup_time = datetime.fromisoformat(created)
                    except (TypeError, ValueError):
                        current_time = backup_time = None
                    if current_time is not None and backup_time is not None and backup_time < current_time:
                        raise ValueError(
                            "This backup is older than the current library. "
                            "Pass force=True to restore it anyway."
                        )
            restored_state = None
            if "library" in restore_items and "library.json" in package.namelist():
                library_file = root / "library.json"
                if library_file.is_file():
                    shutil.copy2(library_file, library_file.with_name("library.before-restore.json"))
                restored_state = default_state()
                try:
                    restored_state.update(json.loads(package.read("library.json")))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("Backup library is invalid.") from error
                if not isinstance(restored_state.get("games"), list):
                    raise ValueError("Backup library is invalid.")
            if "settings" in restore_items and "settings.json" in package.namelist():
                try:
                    archived_settings = json.loads(package.read("settings.json"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("Backup settings are invalid.") from error
                if not isinstance(archived_settings, dict):
                    raise ValueError("Backup settings are invalid.")
                if restored_state is not None:
                    settings = restored_state.setdefault("settings", {})
                    if not isinstance(settings, dict):
                        settings = {}
                        restored_state["settings"] = settings
                    settings.update(archived_settings)
                else:
                    with package.open("settings.json") as source:
                        atomic_copy_stream(source, root / "settings.json", mode=0o600, max_bytes=MAX_BACKUP_MEMBER_BYTES)
            if restored_state is not None:
                with package.open("library.json") as source:
                    atomic_copy_stream(source, library_file, mode=0o600, max_bytes=MAX_BACKUP_MEMBER_BYTES)
            if "settings" in restore_items and "settings.json" in package.namelist():
                with package.open("settings.json") as source:
                    atomic_copy_stream(source, root / "settings.json", mode=0o600, max_bytes=MAX_BACKUP_MEMBER_BYTES)
            for key in ("media", "plugins", "themes", "extension_data"):
                if key not in restore_items:
                    continue
                prefix = f"{BACKUP_ITEMS[key]}/"
                members = [name for name in package.namelist() if name.startswith(prefix) and not name.endswith("/")]
                raw_root = root / BACKUP_ITEMS[key]
                if raw_root.is_symlink():
                    raise ValueError("Backup destination contains a symlink.")
                target_root = raw_root.resolve()
                target_root.mkdir(parents=True, exist_ok=True)
                for member in members:
                    relative = member[len(prefix) :]
                    target = (target_root / relative).resolve()
                    try:
                        target.relative_to(target_root)
                    except ValueError as error:
                        raise ValueError("Backup contains an unsafe path.") from error
                    if any(parent.is_symlink() for parent in [target, *target.parents] if parent != target_root and parent.exists()):
                        raise ValueError("Backup destination contains a symlink.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Re-validate after mkdir: a planted symlink must not redirect the write.
                    resolved = target.resolve()
                    try:
                        resolved.relative_to(target_root)
                    except ValueError as error:
                        raise ValueError("Backup contains an unsafe path.") from error
                    if any(
                        parent.is_symlink()
                        for parent in [resolved, *resolved.parents]
                        if parent != target_root and parent.exists()
                    ):
                        raise ValueError("Backup destination contains a symlink.")
                    with package.open(member) as source:
                        atomic_copy_stream(source, target, mode=0o600, max_bytes=MAX_BACKUP_MEMBER_BYTES)
    except zipfile.BadZipFile as error:
        raise ValueError("Backup archive is invalid.") from error
    return restore_items
