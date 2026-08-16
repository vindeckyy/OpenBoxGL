"""Granular library backups with rotation."""

import json
import os
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from backend_io import atomic_copy_stream, fsync_directory
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
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S-%f")
    return Path(data_dir) / "backups" / f"{prefix}-{stamp}.zip"


def _reject_symlink_components(path):
    path = Path(os.path.abspath(os.fspath(path)))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Backup path may not contain symlinks: {component}")


def _restrict_archive_permissions(folder, pattern):
    for existing in Path(folder).glob(pattern):
        if existing.is_symlink():
            continue
        os.chmod(existing, 0o600)


def _write_archive_file(package, source, archive_name):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ValueError(f"Backup source could not be opened safely: {source}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Backup source is not a regular file: {source}")
        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            with package.open(archive_name, "w") as output:
                while True:
                    chunk = input_file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_archive(archive, populate):
    archive = Path(archive)
    _reject_symlink_components(archive.parent)
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(archive.parent)
    os.chmod(archive.parent, 0o700)
    _restrict_archive_permissions(archive.parent, "OpenBoxBackup-*.zip")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
    )
    temporary = Path(temporary_name)
    os.chmod(temporary, 0o600)
    try:
        with os.fdopen(descriptor, "w+b") as output:
            descriptor = -1
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
                populate(package)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, archive)
        os.chmod(archive, 0o600)
        fsync_directory(archive.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def create_backup(data_dir, state, items, keep=0, running_map=None):
    if running_map is not None and games_running(running_map):
        raise ValueError("Close running games before creating a backup.")
    selected = normalize_items(items)
    root = Path(data_dir)
    _reject_symlink_components(root)
    archive = backup_path(root)
    manifest = {"items": selected, "created": datetime.now().isoformat(timespec="seconds")}

    def populate(package):
        package.writestr("manifest.json", json.dumps(manifest, indent=2))
        if "settings" in selected:
            package.writestr("settings.json", json.dumps(settings_snapshot(state), indent=2))
        if "library" in selected:
            package.writestr("library.json", json.dumps(state, indent=2))
        for key in ("media", "plugins", "themes", "extension_data"):
            if key not in selected:
                continue
            folder = root / BACKUP_ITEMS[key]
            _reject_symlink_components(folder)
            if not folder.exists():
                continue
            if folder.is_symlink():
                raise ValueError(f"Backup source is a symlink: {folder}")
            for path in folder.rglob("*"):
                if path.is_symlink():
                    raise ValueError(f"Backup source contains a symlink: {path}")
                if path.is_file():
                    _write_archive_file(
                        package,
                        path,
                        f"{BACKUP_ITEMS[key]}/{path.relative_to(folder).as_posix()}",
                    )

    _write_private_archive(archive, populate)
    if keep and keep > 0:
        rotate_backups(root / "backups", keep)
    return archive


def rotate_backups(folder, keep):
    if keep < 1:
        raise ValueError("Backup retention must be at least 1.")
    folder = Path(folder)
    _reject_symlink_components(folder)
    archives = []
    for path in folder.glob("OpenBoxBackup-*.zip"):
        if path.is_symlink() or not path.is_file():
            continue
        archives.append(path)
    archives.sort(key=lambda path: path.stat().st_mtime)
    for path in archives[:-keep]:
        path.unlink(missing_ok=True)


def restore_backup(archive_path, data_dir, items=None, running_map=None, force=False):
    if running_map is not None and games_running(running_map):
        raise ValueError("Close running games before restoring a backup.")
    selected = normalize_items(items) if items else None
    root = Path(data_dir)
    _reject_symlink_components(root)
    archive_path = Path(archive_path)
    _reject_symlink_components(archive_path)
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
                    _reject_symlink_components(library_file)
                    with library_file.open("rb") as source:
                        atomic_copy_stream(
                            source,
                            library_file.with_name("library.before-restore.json"),
                            mode=0o600,
                            max_bytes=MAX_BACKUP_MEMBER_BYTES,
                        )
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
                _reject_symlink_components(raw_root)
                target_root = raw_root.resolve()
                target_root.mkdir(parents=True, exist_ok=True)
                _reject_symlink_components(raw_root)
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
