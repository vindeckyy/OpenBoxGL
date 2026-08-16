"""Per-game save backup and restore."""

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from backend_io import atomic_copy_stream, fsync_directory


MAX_SAVE_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_SAVE_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
MAX_SAVE_ARCHIVE_MEMBERS = 50_000


def _reject_symlink_components(path):
    path = Path(os.path.abspath(os.fspath(path)))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Save path may not contain symlinks: {component}")


def _write_archive_file(package, source, archive_name):
    """Copy a file opened without following a symlink into a ZIP member."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ValueError(f"Save backup source could not be opened safely: {source}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Save backup source is not a regular file: {source}")
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


def _restrict_archive_permissions(folder):
    for existing in Path(folder).glob("*.zip"):
        if existing.is_symlink():
            continue
        os.chmod(existing, 0o600)


def _write_private_archive(archive, populate):
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if archive.parent.is_symlink():
        raise ValueError("Save backup directory may not be a symlink.")
    os.chmod(archive.parent, 0o700)
    _restrict_archive_permissions(archive.parent)
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


def game_backup_dir(game, root):
    key = hashlib.sha256(f"{game.get('name','')}:{game.get('path','')}".encode()).hexdigest()[:16]
    return Path(root) / key


def save_roots(game):
    configured = game.get("save_paths", [])
    if not isinstance(configured, list):
        return []
    roots = []
    for path in configured:
        if not str(path).strip():
            continue
        raw_path = Path(path).expanduser()
        _reject_symlink_components(raw_path)
        roots.append(raw_path.resolve())
    return roots


def discover_save_paths(game, home=None):
    home = Path(home or Path.home())
    candidates = []
    app_id = str(game.get("steam_app_id", ""))
    if app_id.isdigit():
        for steam in (
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ):
            candidates.extend(
                {"path":str(path), "label":"Steam Cloud", "shared":False}
                for path in (steam / "userdata").glob(f"*/{app_id}/remote")
                if path.is_dir()
            )
    platform = str(game.get("platform", ""))
    shared = {
        "PlayStation 2": [
            home / ".config/PCSX2/memcards",
            home / ".var/app/net.pcsx2.PCSX2/config/PCSX2/memcards",
        ],
        "PSP": [
            home / ".config/ppsspp/PSP/SAVEDATA",
            home / ".var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SAVEDATA",
        ],
        "PlayStation 3": [
            home / ".config/rpcs3/dev_hdd0/home/00000001/savedata",
            home / ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/home/00000001/savedata",
        ],
        "GameCube": [
            home / ".local/share/dolphin-emu/GC",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC",
        ],
        "Wii": [
            home / ".local/share/dolphin-emu/Wii/title",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title",
        ],
        "WiiWare": [
            home / ".local/share/dolphin-emu/Wii/title",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title",
        ],
        "Sega Saturn": [
            home / ".config/retroarch/saves",
            home / ".var/app/org.libretro.RetroArch/config/retroarch/saves",
            home / ".mednafen",
        ],
        "Wii U": [home / ".local/share/Cemu/mlc01/usr/save"],
    }
    candidates.extend(
        {"path":str(path), "label":f"{platform} shared saves", "shared":True}
        for path in shared.get(platform, [])
        if path.exists()
    )
    title = re.sub(r"[^a-z0-9]+", "", str(game.get("name") or Path(game.get("path", "")).stem).casefold())
    retro_roots = (
        home / ".config/retroarch/saves",
        home / ".config/retroarch/states",
        home / ".var/app/org.libretro.RetroArch/config/retroarch/saves",
        home / ".var/app/org.libretro.RetroArch/config/retroarch/states",
    )
    for root in retro_roots:
        if not root.is_dir() or not title:
            continue
        for path in root.rglob("*"):
            if path.is_file() and re.sub(r"[^a-z0-9]+", "", path.stem.casefold()) == title:
                candidates.append({"path":str(path), "label":"RetroArch save", "shared":False})
    unique = {}
    for candidate in candidates:
        unique[candidate["path"]] = candidate
    return list(unique.values())


def backup_saves(game, root, label="manual"):
    raw_root = Path(root).expanduser()
    _reject_symlink_components(raw_root)
    root = raw_root.resolve()
    roots = [path for path in save_roots(game) if path.exists()]
    if not roots:
        raise FileNotFoundError("No configured save paths currently exist.")
    if any(path.is_symlink() for path in roots):
        raise ValueError("Save backup paths may not be symlinks.")
    directory = game_backup_dir(game, root)
    _reject_symlink_components(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory.parent, 0o700)
    os.chmod(directory, 0o700)
    _restrict_archive_permissions(directory)
    label = str(label or "manual").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", label):
        raise ValueError("Save backup label is invalid.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive = directory / f"{stamp}-{label}.zip"
    manifest = {"game": game.get("name", ""), "roots": [{"path":str(path), "file":path.is_file()} for path in roots]}

    def populate(package):
        package.writestr("manifest.json", json.dumps(manifest))
        for index, source in enumerate(roots):
            if source.is_file():
                _write_archive_file(package, source, f"roots/{index}/{source.name}")
            else:
                _reject_symlink_components(source)
                for file in source.rglob("*"):
                    if file.is_symlink():
                        raise ValueError(f"Save backup source contains a symlink: {file}")
                    if file.is_file():
                        _write_archive_file(package, file, f"roots/{index}/{file.relative_to(source)}")

    _write_private_archive(archive, populate)
    return archive


def list_backups(game, root):
    raw_root = Path(root).expanduser()
    _reject_symlink_components(raw_root)
    directory = game_backup_dir(game, raw_root.resolve())
    _reject_symlink_components(directory)
    if not directory.is_dir() or directory.is_symlink():
        return []
    return sorted(
        (path for path in directory.glob("*.zip") if path.is_file() and not path.is_symlink()),
        reverse=True,
    )


def _validate_save_archive_entries(package):
    infos = package.infolist()
    if len(infos) > MAX_SAVE_ARCHIVE_MEMBERS:
        raise ValueError("Save backup contains too many files.")
    total_bytes = 0
    seen = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        if not name or "\x00" in name or Path(name).is_absolute() or ".." in Path(name).parts or name in seen:
            raise ValueError("Save backup contains an unsafe path.")
        seen.add(name)
        if info.file_size > MAX_SAVE_ARCHIVE_MEMBER_BYTES:
            raise ValueError("Save backup member is too large.")
        total_bytes += info.file_size
        if total_bytes > MAX_SAVE_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Save backup expands beyond the allowed size.")
    return infos


def _load_save_manifest(package):
    try:
        with package.open("manifest.json") as source:
            manifest_bytes = source.read(1024 * 1024 + 1)
        if len(manifest_bytes) > 1024 * 1024:
            raise ValueError("Save backup manifest is too large.")
        manifest = json.loads(manifest_bytes)
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Save backup manifest is invalid.") from error
    return manifest


def _match_save_roots(manifest, configured):
    saved_roots = manifest.get("roots", []) if isinstance(manifest, dict) else []
    if not isinstance(saved_roots, list) or len(saved_roots) != len(configured):
        raise ValueError("Save backup roots do not match this game.")
    roots = []
    for index, item in enumerate(saved_roots):
        if not isinstance(item, dict):
            raise ValueError("Save backup roots do not match this game.")
        saved_path = str(item.get("path", ""))
        if Path(saved_path).expanduser() != configured[index]:
            raise ValueError("Save backup roots do not match this game.")
        expected_file = configured[index].is_file()
        if bool(item.get("file")) != expected_file:
            raise ValueError("Save backup root type does not match this game.")
        _reject_symlink_components(configured[index])
        roots.append({"path": configured[index], "file": expected_file})
    return roots


def _compute_save_destinations(infos, roots):
    destinations = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if info.is_dir() or not name.startswith("roots/"):
            continue
        parts = Path(name).parts
        if len(parts) < 3:
            continue
        try:
            index = int(parts[1])
        except ValueError as error:
            raise ValueError("Invalid save backup manifest.") from error
        if index < 0 or index >= len(roots):
            raise ValueError("Invalid save backup manifest.")
        root_info = roots[index]
        base = (root_info["path"].parent if root_info["file"] else root_info["path"]).resolve()
        _reject_symlink_components(root_info["path"])
        _reject_symlink_components(base)
        destination = (base / Path(*parts[2:])).resolve()
        if destination != base and base not in destination.parents:
            raise ValueError("Save backup contains an unsafe path.")
        raw_destination = base / Path(*parts[2:])
        if raw_destination.is_symlink() or any(parent.is_symlink() for parent in raw_destination.parents if parent != base):
            raise ValueError("Save restore destination contains a symlink.")
        destinations.append((info, destination, base))
    return destinations


def _write_save_restores(package, destinations):
    for info, destination, base in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Re-validate after mkdir: a planted symlink must not redirect the restore write.
        resolved = destination.resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError("Save backup contains an unsafe path.")
        if resolved.is_symlink() or any(parent.is_symlink() for parent in resolved.parents if parent != base):
            raise ValueError("Save restore destination contains a symlink.")
        with package.open(info) as source:
            atomic_copy_stream(source, destination, mode=0o600, max_bytes=MAX_SAVE_ARCHIVE_MEMBER_BYTES)


def restore_saves(game, root, backup_name):
    raw_root = Path(root).expanduser()
    _reject_symlink_components(raw_root)
    root = raw_root.resolve()
    directory = game_backup_dir(game, root)
    _reject_symlink_components(directory)
    archive = directory / Path(backup_name).name
    _reject_symlink_components(archive)
    if directory not in archive.parents or not archive.is_file():
        raise FileNotFoundError("Save backup not found.")
    with zipfile.ZipFile(archive) as package:
        infos = _validate_save_archive_entries(package)
        manifest = _load_save_manifest(package)
        configured = save_roots(game)
        roots = _match_save_roots(manifest, configured)
        destinations = _compute_save_destinations(infos, roots)
        backup_saves(game, root, "before-restore")
        _write_save_restores(package, destinations)
    return archive
