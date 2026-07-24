"""Import and emulator-parity helpers for LaunchBox-depth workflows."""

from __future__ import annotations

import configparser
import re
import struct
from datetime import datetime
from pathlib import Path


EXTENSIONS_EXTRA = {
    ".m3u", ".cue", ".chd", ".wbfs", ".rvz", ".n64", ".z64", ".v64", ".nds",
    ".3ds", ".cia", ".pbp", ".vpk", ".xci", ".nsp", ".wad", ".ciso", ".gcm",
    ".gcz", ".wia", ".dol", ".elf", ".xex", ".iso",
}

PLATFORM_BY_EXTENSION_EXTRA = {
    ".nes": "NES", ".sfc": "SNES", ".smc": "SNES", ".gba": "Game Boy Advance",
    ".gb": "Game Boy", ".gbc": "Game Boy Color", ".n64": "Nintendo 64",
    ".z64": "Nintendo 64", ".v64": "Nintendo 64", ".nds": "Nintendo DS",
    ".3ds": "Nintendo 3DS", ".cia": "Nintendo 3DS", ".wbfs": "Wii",
    ".rvz": "GameCube", ".gcm": "GameCube", ".gcz": "GameCube", ".wia": "Wii",
    ".wad": "WiiWare", ".pbp": "PSP", ".vpk": "PlayStation Vita",
    ".xci": "Nintendo Switch", ".nsp": "Nintendo Switch", ".chd": "Disc image",
    ".cue": "Disc image", ".ciso": "Disc image", ".xex": "Xbox 360",
    ".iso": "Disc image", ".m3u": "Disc image",
}

PLATFORM_EMULATORS = {
    "NES": [("org.libretro.RetroArch", "RetroArch")],
    "SNES": [("org.libretro.RetroArch", "RetroArch")],
    "Nintendo 64": [("org.libretro.RetroArch", "RetroArch")],
    "Game Boy": [("org.libretro.RetroArch", "RetroArch")],
    "Game Boy Color": [("org.libretro.RetroArch", "RetroArch")],
    "Game Boy Advance": [("org.libretro.RetroArch", "RetroArch")],
    "Nintendo DS": [("net.kuribo64.melonDS", "melonDS"), ("org.libretro.RetroArch", "RetroArch")],
    "GameCube": [("org.DolphinEmu.dolphin-emu", "Dolphin")],
    "Wii": [("org.DolphinEmu.dolphin-emu", "Dolphin")],
    "WiiWare": [("org.DolphinEmu.dolphin-emu", "Dolphin")],
    "Wii U": [("info.cemu.Cemu", "Cemu")],
    "PSP": [("org.ppsspp.PPSSPP", "PPSSPP"), ("org.libretro.RetroArch", "RetroArch")],
    "PlayStation": [("org.duckstation.DuckStation", "DuckStation"), ("org.libretro.RetroArch", "RetroArch")],
    "PlayStation 2": [("net.pcsx2.PCSX2", "PCSX2")],
    "PlayStation 3": [("net.rpcs3.RPCS3", "RPCS3")],
    "PlayStation Vita": [("org.vita3k.Vita3K", "Vita3K")],
    "Arcade": [("org.mamedev.MAME", "MAME"), ("org.libretro.RetroArch", "RetroArch")],
    "Xbox": [("app.xemu.xemu", "xemu")],
    "Xbox 360": [("org.xenia.Xenia", "Xenia"), ("org.libretro.RetroArch", "RetroArch")],
    "ScummVM": [("org.scummvm.ScummVM", "ScummVM")],
    "Sega Saturn": [("org.libretro.RetroArch", "RetroArch")],
}

BIOS_HINTS = {
    "DuckStation": [
        ("PSX BIOS (scph1001.bin)", Path.home() / ".local/share/duckstation/bios"),
        ("PSX BIOS (scph1001.bin)", Path.home() / ".var/app/org.duckstation.DuckStation/data/duckstation/bios"),
    ],
    "PCSX2": [
        ("PS2 BIOS folder", Path.home() / ".config/PCSX2/bios"),
        ("PS2 BIOS folder", Path.home() / ".var/app/net.pcsx2.PCSX2/config/PCSX2/bios"),
    ],
    "RPCS3": [
        ("PS3 firmware (dev_flash)", Path.home() / ".config/rpcs3/dev_flash"),
        ("PS3 firmware (dev_flash)", Path.home() / ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_flash"),
    ],
    "RetroArch": [
        ("System/BIOS directory", Path.home() / ".config/retroarch/system"),
        ("System/BIOS directory", Path.home() / ".var/app/org.libretro.RetroArch/config/retroarch/system"),
    ],
}


DISC_RE = re.compile(
    r"[\s._-]*(?:\(|\[)?(?:disc|disk|cd|dvd|side)\s*([0-9a-d]+)(?:\)|\])?$",
    re.I,
)


def recommend_emulators(platform: str):
    items = PLATFORM_EMULATORS.get(platform, [])
    return [{"app_id": app_id, "name": name, "platform": platform} for app_id, name in items]


def generate_m3u(disc_paths, output_path):
    output = Path(output_path)
    lines = []
    for path in disc_paths:
        path = Path(path)
        try:
            lines.append(str(path.relative_to(output.parent)))
        except ValueError:
            lines.append(str(path))
    output.write_text("\n".join(lines) + "\n")
    return output


def _disc_base(path: Path):
    stem = path.stem
    cleaned = DISC_RE.sub("", stem).strip(" ._-\t")
    return cleaned or stem


def group_multi_disc(paths):
    groups = {}
    singles = []
    for path in sorted(Path(p) for p in paths):
        if not DISC_RE.search(path.stem):
            singles.append(path)
            continue
        key = (_disc_base(path).casefold(), path.suffix.lower())
        groups.setdefault(key, []).append(path)
    result = []
    for paths_group in groups.values():
        if len(paths_group) > 1:
            result.append(sorted(paths_group, key=lambda p: p.name.casefold()))
        else:
            singles.extend(paths_group)
    result.extend([[path] for path in singles])
    return result


def import_multi_platform(folder, extensions_set, platform_map):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    found = sorted(
        path for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions_set
    )
    additions = []
    now = datetime.now().isoformat(timespec="seconds")
    for group in group_multi_disc(found):
        if len(group) > 1:
            base = _disc_base(group[0])
            m3u = group[0].with_name(f"{base}.m3u")
            generate_m3u(group, m3u)
            path = m3u
            name = base
        else:
            path = group[0]
            name = path.stem
        platform = platform_map.get(path.suffix.lower(), "Imported")
        if path.suffix.lower() == ".m3u":
            platform = platform_map.get(group[0].suffix.lower(), platform)
        additions.append({
            "name": name,
            "platform": platform,
            "genre": "",
            "path": str(path),
            "added_at": now,
            "discs": [str(item) for item in group] if len(group) > 1 else [],
        })
    return dedupe_ranked_imports(additions)


def dedupe_ranked_imports(additions):
    from parity_premium import pick_best_rom, rank_rom_group

    buckets = {}
    for item in additions:
        key = (
            item.get("platform", ""),
            re.sub(r"[^a-z0-9]+", " ", str(item.get("name", "")).casefold()).strip(),
        )
        buckets.setdefault(key, []).append(item)
    result = []
    for items in buckets.values():
        if len(items) == 1:
            result.append(items[0])
            continue
        paths = [entry["path"] for entry in items]
        best = pick_best_rom(paths)
        winner = next(entry for entry in items if entry["path"] == best)
        winner["version_candidates"] = rank_rom_group(paths)
        result.append(winner)
    return result


def import_scummvm(home=None):
    home = Path(home or Path.home())
    ini_paths = [
        home / ".config/scummvm/scummvm.ini",
        home / ".var/app/org.scummvm.ScummVM/config/scummvm/scummvm.ini",
    ]
    games = []
    for ini_path in ini_paths:
        if not ini_path.is_file():
            continue
        parser = configparser.ConfigParser()
        parser.read(ini_path)
        for section in parser.sections():
            if section in {"scummvm", "cloud"}:
                continue
            description = parser.get(section, "description", fallback=section)
            path = parser.get(section, "path", fallback="")
            games.append({
                "name": description,
                "platform": "ScummVM",
                "genre": "Adventure",
                "path": path or str(ini_path),
                "launch": f"scummvm {section}",
                "source": "scummvm",
                "scummvm_id": section,
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
        if games:
            break
    return games


def _read_sfo_title(param_sfo: Path):
    try:
        data = param_sfo.read_bytes()
        if data[0:4] != b"\x00PSF":
            return param_sfo.parent.name
        key_table, data_table, entry_count = struct.unpack_from("<III", data, 8)
        for index in range(entry_count):
            key_offset, data_fmt, data_len, data_max, data_offset = struct.unpack_from(
                "<HHIII", data, 20 + index * 16
            )
            key = data[key_table + key_offset:].split(b"\x00", 1)[0].decode("utf-8", "ignore")
            if key == "TITLE" and data_fmt in {0x0204, 0x0402}:
                raw = data[data_table + data_offset:data_table + data_offset + data_len]
                return raw.split(b"\x00", 1)[0].decode("utf-8", "ignore") or param_sfo.parent.name
    except (OSError, struct.error, ValueError):
        pass
    return param_sfo.parent.name


def import_rpcs3_hdd(home=None):
    home = Path(home or Path.home())
    roots = [
        home / ".config/rpcs3/dev_hdd0/game",
        home / ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/game",
    ]
    games = []
    for root in roots:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            sfo = folder / "PARAM.SFO"
            if not folder.is_dir() or not sfo.is_file():
                continue
            title = _read_sfo_title(sfo)
            games.append({
                "name": title,
                "platform": "PlayStation 3",
                "genre": "",
                "path": str(folder),
                "launch": f"rpcs3 {folder}",
                "source": "rpcs3",
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
        if games:
            break
    return games


def import_vita3k(home=None):
    home = Path(home or Path.home())
    roots = [
        home / ".local/share/Vita3K/Vita3K/ux0/app",
        home / ".config/Vita3K/ux0/app",
        home / ".var/app/org.vita3k.Vita3K/data/Vita3K/Vita3K/ux0/app",
    ]
    games = []
    for root in roots:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir():
                continue
            sfo = folder / "sce_sys" / "param.sfo"
            entry = {
                "title": _read_sfo_title(sfo) if sfo.is_file() else folder.name,
                "title_id": folder.name,
                "metadata": {"Title": _read_sfo_title(sfo) if sfo.is_file() else folder.name},
            }
            from parity_premium import resolve_vita_title
            title = resolve_vita_title(entry)
            games.append({
                "name": title,
                "platform": "PlayStation Vita",
                "genre": "",
                "path": str(folder),
                "source": "vita3k",
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
        if games:
            break
    return games


def detect_dependencies(emulator_name, home=None):
    home = Path(home or Path.home())
    hints = BIOS_HINTS.get(emulator_name, [])
    required, missing = [], []
    for label, path in hints:
        path = Path(str(path).replace(str(Path.home()), str(home)))
        found = path.exists() and (path.is_file() or any(path.iterdir()) if path.is_dir() else False)
        entry = {"name": label, "path": str(path), "found": bool(found)}
        required.append(entry)
        if not found:
            missing.append(entry)
    return {"required": required, "missing": missing}
