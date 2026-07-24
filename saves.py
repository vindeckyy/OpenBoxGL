"""Per-game save backup and restore."""

import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path


def game_backup_dir(game, root):
    key = hashlib.sha256(f"{game.get('name','')}:{game.get('path','')}".encode()).hexdigest()[:16]
    return Path(root) / key


def save_roots(game):
    return [Path(path).expanduser() for path in game.get("save_paths", []) if str(path).strip()]


def discover_save_paths(game, home=Path.home()):
    home = Path(home)
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
    roots = [path for path in save_roots(game) if path.exists()]
    if not roots:
        raise FileNotFoundError("No configured save paths currently exist.")
    directory = game_backup_dir(game, root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive = directory / f"{stamp}-{label}.zip"
    manifest = {"game": game.get("name", ""), "roots": [{"path":str(path), "file":path.is_file()} for path in roots]}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("manifest.json", json.dumps(manifest))
        for index, source in enumerate(roots):
            if source.is_file():
                package.write(source, f"roots/{index}/{source.name}")
            else:
                for file in source.rglob("*"):
                    if file.is_file():
                        package.write(file, f"roots/{index}/{file.relative_to(source)}")
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
        manifest = json.loads(package.read("manifest.json"))
        roots = [
            {"path":Path(item["path"]).expanduser(), "file":bool(item.get("file"))}
            if isinstance(item, dict) else {"path":Path(item).expanduser(), "file":False}
            for item in manifest.get("roots", [])
        ]
        for info in package.infolist():
            if info.is_dir() or not info.filename.startswith("roots/"):
                continue
            parts = Path(info.filename).parts
            if len(parts) < 3:
                continue
            index = int(parts[1])
            if index >= len(roots):
                raise ValueError("Invalid save backup manifest.")
            root = roots[index]
            destination = (root["path"].parent if root["file"] else root["path"]) / Path(*parts[2:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(package.read(info))
    return archive
