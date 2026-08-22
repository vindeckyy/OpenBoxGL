"""Import helpers for M3U, multi-platform folders, emulator recommendations, and ROM ranking."""

from __future__ import annotations

import collections
import configparser
import os
import re
import struct
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from backend_io import atomic_write_text


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

def _get_bios_hints():
    """Return BIOS hints with runtime Path.home() calls (not frozen at import)."""
    home = Path.home()
    return {
        "DuckStation": [
            ("PSX BIOS (scph1001.bin)", home / ".local/share/duckstation/bios"),
            ("PSX BIOS (scph1001.bin)", home / ".var/app/org.duckstation.DuckStation/data/duckstation/bios"),
        ],
        "PCSX2": [
            ("PS2 BIOS folder", home / ".config/PCSX2/bios"),
            ("PS2 BIOS folder", home / ".var/app/net.pcsx2.PCSX2/config/PCSX2/bios"),
        ],
        "RPCS3": [
            ("PS3 firmware (dev_flash)", home / ".config/rpcs3/dev_flash"),
            ("PS3 firmware (dev_flash)", home / ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_flash"),
        ],
        "RetroArch": [
            ("System/BIOS directory", home / ".config/retroarch/system"),
            ("System/BIOS directory", home / ".var/app/org.libretro.RetroArch/config/retroarch/system"),
        ],
    }


BIOS_HINTS = _get_bios_hints()


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
    atomic_write_text(output, "\n".join(lines) + "\n")
    return output


def parse_m3u(m3u_path: Path | str) -> list[Path]:
    """Safely parse an M3U file, returning referenced paths resolved relative to the M3U."""
    path = Path(m3u_path)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            line_path = Path(line)
            target = (path.parent / line_path).resolve() if not line_path.is_absolute() else line_path
            entries.append(target)
        except (ValueError, OSError):
            continue
    return entries


def parse_cue(cue_path: Path | str) -> list[Path]:
    """Safely parse a CUE sheet, returning referenced FILE target paths."""
    path = Path(cue_path)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for match in re.finditer(r'FILE\s+["\']?([^"\']+)["\']?\s+BINARY', content, re.I):
        filename = match.group(1).strip()
        if filename:
            try:
                fn_path = Path(filename)
                target = (path.parent / fn_path).resolve() if not fn_path.is_absolute() else fn_path
                entries.append(target)
            except (ValueError, OSError):
                continue
    return entries


def _disc_base(path: Path | str) -> str:
    p = path if isinstance(path, Path) else Path(path)
    stem = p.stem
    cleaned = DISC_RE.sub("", stem).strip(" ._-\t")
    return cleaned or stem


DISC_TOKENS = ("disc", "disk", "cd", "dvd", "side")


def group_multi_disc(paths: Iterable[Path | str]) -> list[list[Path]]:
    groups: dict[tuple[Path, str, str], list[Path]] = {}
    singles: list[Path] = []
    for p in paths:
        path = p if isinstance(p, Path) else Path(p)
        name = path.name
        idx = name.rfind(".")
        if idx != -1:
            stem = name[:idx]
            suffix = name[idx:].casefold()
        else:
            stem = name
            suffix = ""
        stem_lower = stem.casefold()
        if (
            "disc" not in stem_lower
            and "disk" not in stem_lower
            and "cd" not in stem_lower
            and "dvd" not in stem_lower
            and "side" not in stem_lower
        ):
            singles.append(path)
            continue
        match = DISC_RE.search(stem)
        if not match:
            singles.append(path)
            continue
        base = stem[:match.start()].strip(" ._-\t").casefold() or stem_lower
        key = (path.parent, base, suffix)
        groups.setdefault(key, []).append(path)
    result: list[list[Path]] = []
    for paths_group in groups.values():
        if len(paths_group) > 1:
            result.append(sorted(paths_group, key=lambda p: p.name.casefold()))
        else:
            singles.extend(paths_group)
    result.extend([[path] for path in singles])
    return result



def _parallel_scandir(
    folder: Path | str,
    extensions_set: Iterable[str],
    max_workers: int | None = None,
    progress_callback: Callable | None = None,
) -> list[Path]:
    """Parallel multi-core directory crawler using os.scandir and worker pool."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")

    ext_set = {
        ext.casefold() if ext.startswith(".") else f".{ext.casefold()}"
        for ext in extensions_set
    }

    found_paths: list[Path] = []
    found_lock = threading.Lock()

    seen_dirs: set[str] = set()
    try:
        seen_dirs.add(os.path.realpath(str(root)))
    except OSError:
        seen_dirs.add(str(root))

    dirs_queue: collections.deque[str] = collections.deque([str(root)])
    queue_lock = threading.Lock()
    cv = threading.Condition(queue_lock)
    active_workers = 0
    done = False

    workers_count = max_workers or min(16, max(2, (os.cpu_count() or 2) * 2))

    def worker_loop():
        nonlocal active_workers, done
        while True:
            with cv:
                while not dirs_queue and not done:
                    if active_workers == 0:
                        done = True
                        cv.notify_all()
                        break
                    cv.wait(timeout=0.05)
                if done and not dirs_queue:
                    break
                if not dirs_queue:
                    continue
                current_dir = dirs_queue.popleft()
                active_workers += 1

            child_dirs: list[str] = []
            child_files: list[Path] = []
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                try:
                                    real_path = os.path.realpath(entry.path)
                                except OSError:
                                    real_path = entry.path
                                with queue_lock:
                                    if real_path not in seen_dirs:
                                        seen_dirs.add(real_path)
                                        child_dirs.append(entry.path)
                            elif entry.is_file(follow_symlinks=True):
                                name = entry.name
                                idx = name.rfind(".")
                                if idx != -1 and name[idx:].casefold() in ext_set:
                                    child_files.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                pass

            with cv:
                if child_dirs:
                    dirs_queue.extend(child_dirs)
                    cv.notify_all()
                active_workers -= 1
                if active_workers == 0 and not dirs_queue:
                    done = True
                    cv.notify_all()

            if child_files:
                with found_lock:
                    found_paths.extend(child_files)
                    if progress_callback:
                        try:
                            progress_callback(found_count=len(found_paths))
                        except Exception:
                            pass

    threads = [threading.Thread(target=worker_loop, daemon=True) for _ in range(workers_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return sorted(found_paths, key=lambda p: (str(p.parent), p.name.casefold()))


def import_multi_platform(
    folder: Path | str,
    extensions_set: Iterable[str],
    platform_map: dict[str, str],
    progress_callback: Callable | None = None,
) -> list[dict]:
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    found = _parallel_scandir(folder, extensions_set, progress_callback=progress_callback)

    m3u_referenced: set[str] = set()
    for path in found:
        if path.suffix.casefold() == ".m3u":
            for ref in parse_m3u(path):
                try:
                    m3u_referenced.add(str(ref.resolve() if ref.exists() else ref))
                except OSError:
                    m3u_referenced.add(str(ref))

    filtered_found = [
        p for p in found
        if str(p.resolve() if p.exists() else p) not in m3u_referenced
    ] if m3u_referenced else found

    additions = []
    now = datetime.now().isoformat(timespec="seconds")
    disc_groups = group_multi_disc(filtered_found)
    total_groups = len(disc_groups)

    for index, group in enumerate(disc_groups):
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
        if progress_callback and (index % 100 == 0 or index == total_groups - 1):
            try:
                progress_callback(processed_count=index + 1, total_count=total_groups)
            except Exception:
                pass
    return dedupe_ranked_imports(additions)


_ROM_TAGS_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")


def _clean_rom_title(name: str) -> str:
    if "(" in name or "[" in name:
        cleaned = _ROM_TAGS_RE.sub(" ", name)
    else:
        cleaned = name
    norm = _NON_ALPHANUM.sub(" ", cleaned.casefold()).strip()
    return norm or _NON_ALPHANUM.sub(" ", name.casefold()).strip()


def dedupe_ranked_imports(additions: list[dict]) -> list[dict]:
    from parity_premium import rank_rom_group

    buckets: dict[tuple[str, str], list[dict]] = {}
    title_cache: dict[str, str] = {}
    for item in additions:
        name = str(item.get("name", ""))
        norm_title = title_cache.get(name)
        if norm_title is None:
            norm_title = _clean_rom_title(name)
            title_cache[name] = norm_title
        key = (
            item.get("platform", ""),
            norm_title,
        )
        buckets.setdefault(key, []).append(item)
    result = []
    for items in buckets.values():
        if len(items) == 1:
            result.append(items[0])
            continue
        paths = [entry["path"] for entry in items]
        ranked = rank_rom_group(paths)
        best = ranked[0] if ranked else paths[0]
        winner = next((entry for entry in items if entry["path"] == best), items[0])
        winner["version_candidates"] = ranked
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
