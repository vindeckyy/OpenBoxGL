"""Per-game save backup and restore."""

import hashlib
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from backend_io import atomic_copy_stream, fsync_directory


MAX_SAVE_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_SAVE_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024 * 1024


def game_backup_dir(game, root):
    key = hashlib.sha256(f"{game.get('name','')}:{game.get('path','')}".encode()).hexdigest()[:16]
    return Path(root) / key


def save_roots(game):
    return [Path(path).expanduser().resolve() for path in game.get("save_paths", []) if str(path).strip()]


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
    root = Path(root).expanduser().resolve()
    roots = [path for path in save_roots(game) if path.exists()]
    if not roots:
        raise FileNotFoundError("No configured save paths currently exist.")
    if any(path.is_symlink() for path in roots):
        raise ValueError("Save backup paths may not be symlinks.")
    directory = game_backup_dir(game, root)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
        raise ValueError("Save backup directory may not contain symlinks.")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive = directory / f"{stamp}-{label}.zip"
    temporary = archive.with_name(f".{archive.name}.tmp")
    manifest = {"game": game.get("name", ""), "roots": [{"path":str(path), "file":path.is_file()} for path in roots]}
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("manifest.json", json.dumps(manifest))
            for index, source in enumerate(roots):
                if source.is_file():
                    package.write(source, f"roots/{index}/{source.name}")
                else:
                    for file in source.rglob("*"):
                        if file.is_symlink():
                            raise ValueError(f"Save backup source contains a symlink: {file}")
                        if file.is_file():
                            package.write(file, f"roots/{index}/{file.relative_to(source)}")
        os.replace(temporary, archive)
        fsync_directory(archive.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def list_backups(game, root):
    directory = game_backup_dir(game, root)
    return sorted(directory.glob("*.zip"), reverse=True) if directory.is_dir() else []


def restore_saves(game, root, backup_name):
    directory = game_backup_dir(game, root).resolve()
    archive = (directory / Path(backup_name).name).resolve()
    if directory not in archive.parents or not archive.is_file():
        raise FileNotFoundError("Save backup not found.")
    backup_saves(game, root, "before-restore")
    with zipfile.ZipFile(archive) as package:
        try:
            manifest = json.loads(package.read("manifest.json"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Save backup manifest is invalid.") from error
        configured = save_roots(game)
        saved_roots = manifest.get("roots", []) if isinstance(manifest, dict) else []
        if not isinstance(saved_roots, list) or len(saved_roots) != len(configured):
            raise ValueError("Save backup roots do not match this game.")
        roots = []
        for index, item in enumerate(saved_roots):
            saved_path = str(item.get("path", "")) if isinstance(item, dict) else str(item)
            if Path(saved_path).expanduser() != configured[index]:
                raise ValueError("Save backup roots do not match this game.")
            roots.append({"path": configured[index], "file": bool(item.get("file")) if isinstance(item, dict) else False})
        total_bytes = 0
        seen = set()
        for info in package.infolist():
            if info.is_dir() or not info.filename.startswith("roots/"):
                continue
            name = info.filename.replace("\\", "/")
            if "\x00" in name or Path(name).is_absolute() or ".." in Path(name).parts or name in seen:
                raise ValueError("Save backup contains an unsafe path.")
            seen.add(name)
            if info.file_size > MAX_SAVE_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Save backup member is too large.")
            total_bytes += info.file_size
            if total_bytes > MAX_SAVE_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Save backup expands beyond the allowed size.")
            parts = Path(name).parts
            if len(parts) < 3:
                continue
            index = int(parts[1])
            if index >= len(roots):
                raise ValueError("Invalid save backup manifest.")
            root = roots[index]
            base = (root["path"].parent if root["file"] else root["path"]).resolve()
            if root["path"].is_symlink() or any(parent.is_symlink() for parent in root["path"].parents):
                raise ValueError("Save restore destination contains a symlink.")
            destination = (base / Path(*parts[2:])).resolve()
            if destination != base and base not in destination.parents:
                raise ValueError("Save backup contains an unsafe path.")
            raw_destination = base / Path(*parts[2:])
            if raw_destination.is_symlink() or any(parent.is_symlink() for parent in raw_destination.parents if parent != base):
                raise ValueError("Save restore destination contains a symlink.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Re-validate after mkdir: a symlink planted at a previously missing
            # intermediate directory must not redirect the restore write.
            resolved = destination.resolve()
            if resolved != base and base not in resolved.parents:
                raise ValueError("Save backup contains an unsafe path.")
            if resolved.is_symlink() or any(parent.is_symlink() for parent in resolved.parents if parent != base):
                raise ValueError("Save restore destination contains a symlink.")
            with package.open(info) as source:
                atomic_copy_stream(source, destination, mode=0o600, max_bytes=MAX_SAVE_ARCHIVE_MEMBER_BYTES)
    return archive
