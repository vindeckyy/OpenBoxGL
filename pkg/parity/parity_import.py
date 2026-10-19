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

from pkg.parity.parity_emulator_defs import PLATFORM_EMULATORS as _PLATFORM_EMULATORS  # noqa: E402
from pkg.parity.parity_emulator_defs import _parse_yaml  # noqa: E402

PLATFORM_EMULATORS = _PLATFORM_EMULATORS


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


def generated_m3u_dir() -> Path:
    from openbox import APP_DIR

    return APP_DIR / "generated"


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


def _write_m3u_playlist(group, base, m3u_dir):
    """Write a multi-disc playlist, degrading gracefully on unwritable targets.

    Order: the requested directory (or the ROM dir beside the discs) first,
    then the generated playlist dir.  Returns the written ``Path`` or ``None``
    when every target rejects the write — callers then keep the first disc.

    An existing ``.m3u`` is never overwritten, and its presence stops the write
    entirely rather than falling through to the next directory.  The file may be
    hand-made, or stale from an earlier import, but either way it is the user's
    file and destroying it is unrecoverable.  Writing the same name somewhere
    else is worse than not writing: the scan only reads the ROM folder, so a
    playlist in the generated directory is never re-read, and the row would
    point at a file the user never asked for while the original is a second
    row of the same game.  Returning ``None`` makes the caller keep the group's
    first disc as the row's path, and the user's playlist is still scanned, so
    the ROM it references still imports.
    """
    targets = [Path(m3u_dir)] if m3u_dir is not None else [group[0].parent]
    generated = generated_m3u_dir()
    if generated not in targets:
        targets.append(generated)
    for directory in targets:
        candidate = directory / f"{base}.m3u"
        if candidate.exists():
            return None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return generate_m3u(group, candidate)
        except OSError:
            continue
    return None


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
    # The file type token after the name is BINARY in most sheets, but
    # MOTOROLA, WAVE and AIFF are all valid and appear in mixed-mode and
    # legacy tooling. Matching only BINARY let those sheets' data files
    # through as a second import of the same game.
    for match in re.finditer(r'FILE\s+["\']?([^"\']+)["\']?\s+(\w+)', content, re.I):
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


def _sheet_platform(
    sheet: Path,
    platform_map: dict[str, str],
    fallback: str,
    targets: list[Path] | None = None,
) -> str:
    """Resolve the platform a disc sheet's row should carry.

    A sheet only describes its targets, and the useful platform is whatever
    maps to a real emulator. Redump-style sets chain a playlist to cue sheets
    to disc images, so a playlist's targets are followed through the sheets
    they name. A sheet whose targets map to nothing keeps the fallback.
    ``targets`` lets the caller hand over the parse it already did, since the
    exclusion pass reads every sheet anyway.
    """
    pending = [(sheet, targets)]
    seen: set[Path] = set()
    while pending:
        current, current_targets = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        # Only sheets are ever queued: the seed is one, and a target is
        # appended below only when its own suffix is a sheet.
        if current_targets is None:
            current_targets = (
                parse_cue(current) if current.suffix.casefold() == ".cue" else parse_m3u(current)
            )
        for target in current_targets:
            mapped = platform_map.get(target.suffix.casefold())
            if mapped and target.suffix.casefold() not in (".cue", ".m3u"):
                return mapped
            if target.suffix.casefold() in (".cue", ".m3u"):
                pending.append((target, None))
    return fallback


def import_multi_platform(
    folder: Path | str,
    extensions_set: Iterable[str],
    platform_map: dict[str, str],
    progress_callback: Callable | None = None,
    *,
    write_m3u: bool = True,
    m3u_dir: Path | None = None,
) -> list[dict]:
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    found = _parallel_scandir(folder, extensions_set, progress_callback=progress_callback)

    # A .m3u or a .cue sheet points at the files it describes, so those
    # targets are imported through their sheet and not again on their own.
    # A cue and its .bin land in different platform buckets ("Disc image" vs
    # the adapter's own platform), so dedupe_ranked_imports cannot merge them:
    # without this the pair imports as two rows of the same game.
    referenced: set[str] = set()
    sheet_targets: dict[Path, list[Path]] = {}
    for path in found:
        suffix = path.suffix.casefold()
        if suffix == ".m3u":
            refs = parse_m3u(path)
        elif suffix == ".cue":
            refs = parse_cue(path)
        else:
            continue
        sheet_targets[path] = refs
        # parse_m3u and parse_cue resolve relative entries, but hand back an
        # absolute one verbatim, so a sheet may name its target with ".."
        # segments that the resolved scan paths no longer match. Normalizing
        # both sides is what makes the comparison mean the same thing.
        for ref in refs:
            referenced.add(str(Path(os.path.normpath(ref))))
    filtered_found = [
        p for p in found
        if str(Path(os.path.normpath(p.resolve() if p.exists() else p))) not in referenced
    ] if referenced else found

    additions = []
    now = datetime.now().isoformat(timespec="seconds")
    disc_groups = group_multi_disc(filtered_found)
    total_groups = len(disc_groups)

    for index, group in enumerate(disc_groups):
        if len(group) > 1:
            base = _disc_base(group[0])
            if write_m3u:
                m3u = _write_m3u_playlist(group, base, m3u_dir)
                if m3u is not None:
                    # The exclusion pass cached every sheet it read *before* this
                    # write. Leaving the stale entry in place makes _sheet_platform
                    # below resolve the platform from what the file used to point
                    # at, so a sheet we just generated over an old one reported
                    # "Disc image" instead of following its own disc images to the
                    # real platform. Refresh the entry with what was actually
                    # written -- the same list a re-parse would return.
                    sheet_targets[m3u] = [Path(item) for item in group]
                    path = m3u
                else:
                    path = group[0]
            else:
                path = group[0]
            name = base
        else:
            path = group[0]
            name = path.stem
        platform = platform_map.get(path.suffix.lower(), "Imported")
        # A sheet names a disc, but the emulator that matters is the one for
        # a file it points at: "Disc image" recommends nothing, so a sheet left
        # on it offers the user no suggestion. The group cannot be trusted for
        # this -- the sheet's own targets were excluded from the scan, and a
        # shipped playlist that was not written here is a group of one.
        if path.suffix.lower() in (".cue", ".m3u"):
            platform = _sheet_platform(path, platform_map, platform, sheet_targets.get(path))
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


RPCS3_CONFIG_DIRS = (
    Path(".config") / "rpcs3",
    Path(".var") / "app" / "net.rpcs3.RPCS3" / "config" / "rpcs3",
)
RPCS3_GAME_PATH_KEYS = ("path", "Path", "game", "Game")
_RPCS3_ABSOLUTE_RE = re.compile(r"^(?:/|~|[A-Za-z]:[\\/])")


def _rpcs3_looks_like_path(text):
    return isinstance(text, str) and bool(_RPCS3_ABSOLUTE_RE.match(text.strip()))


def _rpcs3_path_value(value):
    if isinstance(value, dict):
        for key in RPCS3_GAME_PATH_KEYS:
            candidate = value.get(key)
            if _rpcs3_looks_like_path(candidate):
                return candidate.strip()
        return ""
    if _rpcs3_looks_like_path(value):
        return value.strip()
    return ""


def _rpcs3_yaml_payload(path, errors):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"{path}: {error}")
        return None
    try:
        return _parse_yaml(text)
    except Exception as error:  # malformed YAML must skip the file, never abort the scan
        errors.append(f"{path}: {error}")
        return None


def _rpcs3_yaml_paths(path, payload):
    """Return the game paths an RPCS3 ``games.yml`` or ``config.yml`` declares.

    ``games.yml`` maps a game path to its metadata (or, in older layouts,
    carries a ``path`` value), while ``config.yml`` keeps a ``games:`` list or
    mapping. Anything that does not look path-shaped is ignored rather than
    imported as a bogus entry.
    """
    paths = []
    if path.name.casefold() == "games.yml":
        if isinstance(payload, dict):
            for key, value in payload.items():
                if _rpcs3_looks_like_path(key):
                    paths.append(key.strip())
                candidate = _rpcs3_path_value(value)
                if candidate:
                    paths.append(candidate)
        elif isinstance(payload, list):
            paths.extend(candidate for candidate in map(_rpcs3_path_value, payload) if candidate)
        return paths
    games = payload.get("games") if isinstance(payload, dict) else None
    if isinstance(games, dict):
        for key, value in games.items():
            candidate = _rpcs3_path_value(value)
            if candidate:
                paths.append(candidate)
            elif _rpcs3_looks_like_path(key):
                paths.append(key.strip())
    elif isinstance(games, list):
        paths.extend(candidate for candidate in map(_rpcs3_path_value, games) if candidate)
    return paths


def _rpcs3_game(path, errors):
    """Return ``(real_path, game)`` for one external entry, or ``None``."""
    text = os.path.expandvars(os.path.expanduser(str(path)))
    expanded = Path(text)
    try:
        expanded.stat()
    except OSError as error:
        errors.append(f"{expanded}: {error}")
        return None
    if expanded.is_dir():
        sfo = expanded / "PARAM.SFO"
        try:
            has_sfo = sfo.is_file()
        except OSError as error:
            errors.append(f"{sfo}: {error}")
            return None
        if not has_sfo:
            errors.append(f"{expanded}: no PARAM.SFO found")
            return None
        name = _read_sfo_title(sfo)
    elif expanded.is_file() and expanded.suffix.casefold() == ".iso":
        name = expanded.stem
    else:
        errors.append(f"{expanded}: not a PlayStation 3 game folder or ISO")
        return None
    try:
        real = os.path.realpath(expanded)
    except OSError as error:
        errors.append(f"{expanded}: {error}")
        return None
    return real, {
        "name": name,
        "platform": "PlayStation 3",
        "genre": "",
        "path": str(expanded),
        "launch": f"rpcs3 {expanded}",
        "source": "rpcs3",
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }


def import_rpcs3_hdd(home=None, errors=None):
    """Import installed games, external folders, and ISOs from RPCS3.

    The ``dev_hdd0/game`` scan stays the primary source. RPCS3 also records
    games installed outside the virtual HDD (and ISOs) in ``games.yml`` or the
    ``games:`` list of ``config.yml``, so both are read for the native and
    Flatpak config directories. An inaccessible entry is reported and skipped;
    one bad path never aborts the scan.
    """
    home = Path(home or Path.home())
    errors = errors if errors is not None else []
    candidates = []
    roots = [
        home / ".config/rpcs3/dev_hdd0/game",
        home / ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/game",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError as error:
            errors.append(f"{root}: {error}")
            continue
        for folder in entries:
            sfo = folder / "PARAM.SFO"
            try:
                if folder.is_dir() and sfo.is_file():
                    candidates.append(folder)
            except OSError as error:
                errors.append(f"{folder}: {error}")

    for config_dir in RPCS3_CONFIG_DIRS:
        for name in ("games.yml", "config.yml"):
            path = home / config_dir / name
            if not path.is_file():
                continue
            payload = _rpcs3_yaml_payload(path, errors)
            if payload is None:
                continue
            candidates.extend(_rpcs3_yaml_paths(path, payload))

    games = []
    seen = set()
    for candidate in candidates:
        item = _rpcs3_game(candidate, errors)
        if item is None:
            continue
        real, game = item
        if real in seen:
            continue
        seen.add(real)
        games.append(game)
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
        try:
            found = path.is_file() or (path.is_dir() and any(path.iterdir()))
        except OSError:
            found = False
        entry = {"name": label, "path": str(path), "found": bool(found)}
        required.append(entry)
        if not found:
            missing.append(entry)
    return {"required": required, "missing": missing}
