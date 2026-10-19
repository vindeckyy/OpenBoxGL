"""Legacy platform importers for PICO-8, MSU-1, and Amiga WHDLoad libraries.

Every importer takes a folder and returns the games it found. Unreadable
entries and unpaired carts are appended to the optional ``errors`` list
instead of aborting the scan, matching the ``import_steam(errors=...)``
convention used by the other importers. Real paths are deduplicated so a
symlinked folder cannot import the same cart twice.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

MSU1_ROM_EXTENSIONS = {".sfc", ".smc"}
WHDLOAD_EXTENSIONS = {".lha", ".slave", ".hdf"}
# WHDLoad packs ship the Amiga system drawers next to the games. Those files
# are required by the installer but are not launchable games, so an extracted
# pack would otherwise import dozens of C/Libs/Devs entries.
WHDLOAD_SYSTEM_DRAWERS = {"c", "libs", "devs"}


def _error_list(errors):
    return errors if errors is not None else []


def _required_root(folder):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {root}")
    return root


def _walk_files(root, errors):
    """Return regular files under ``root`` once per real path, sorted.

    ``os.walk`` is used instead of ``Path.rglob`` so an unreadable directory
    can be reported through ``onerror`` and skipped rather than raising for
    the whole scan.
    """
    seen: dict[str, Path] = {}

    def onerror(error):
        errors.append(f"{getattr(error, 'filename', '') or root}: {error}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        dirnames.sort(key=str.casefold)
        for name in sorted(filenames, key=str.casefold):
            path = Path(dirpath) / name
            try:
                info = path.stat()
            except OSError as error:
                errors.append(f"{path}: {error}")
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            real = os.path.realpath(path)
            existing = seen.get(real)
            # A symlink and its target are one file. Keep the real path so the
            # imported name is the cart's own, not the link's.
            if existing is None or (existing.is_symlink() and not path.is_symlink()):
                seen[real] = path
    return [seen[key] for key in sorted(seen, key=str.casefold)]


def _walk_dirs(root, errors):
    """Return directories under ``root`` that look like MSU-1 audio packs."""
    found: list[Path] = []

    def onerror(error):
        errors.append(f"{getattr(error, 'filename', '') or root}: {error}")

    for dirpath, dirnames, _filenames in os.walk(root, onerror=onerror):
        dirnames.sort(key=str.casefold)
        for name in dirnames:
            if name.casefold().endswith(".msu"):
                found.append(Path(dirpath) / name)
    return found


def _imported_at():
    return datetime.now().isoformat(timespec="seconds")


def import_pico8(folder, errors=None):
    """Import PICO-8 carts (``.p8`` and exported ``.p8.png``) recursively."""
    errors = _error_list(errors)
    root = _required_root(folder)
    games = []
    seen: set[str] = set()
    now = _imported_at()
    for path in _walk_files(root, errors):
        name = path.name
        lower = name.casefold()
        if lower.endswith(".p8.png"):
            title = name[: -len(".p8.png")]
        elif lower.endswith(".p8"):
            title = name[: -len(".p8")]
        else:
            continue
        try:
            real = os.path.realpath(path)
        except OSError as error:
            errors.append(f"{path}: {error}")
            continue
        if real in seen:
            continue
        seen.add(real)
        games.append({
            "name": title,
            "platform": "PICO-8",
            "genre": "",
            "path": str(path),
            "source": "PICO-8",
            "emulator_adapter_id": "pico8-pico8",
            "added_at": now,
        })
    return games


def _sibling_roms(parent: Path, errors) -> dict[str, Path]:
    """Map a case-folded stem to the first ``.sfc``/``.smc`` sibling."""
    roms: dict[str, Path] = {}
    try:
        with os.scandir(parent) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
    except OSError as error:
        errors.append(f"{parent}: {error}")
        return roms
    for entry in entries:
        suffix = Path(entry.name).suffix.casefold()
        if suffix not in MSU1_ROM_EXTENSIONS:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError as error:
            errors.append(f"{entry.path}: {error}")
            continue
        stem = entry.name[: -len(suffix)]
        roms.setdefault(stem.casefold(), Path(entry.path))
    return roms


def import_msu1(folder, errors=None):
    """Import SNES ROMs paired with a sibling ``*.msu`` audio-pack folder.

    The ``.msu`` folder is what makes the ROM an MSU-1 game; a pack with no
    matching ROM is reported and skipped because there is nothing to launch.
    """
    errors = _error_list(errors)
    root = _required_root(folder)
    games = []
    seen: set[str] = set()
    now = _imported_at()
    for path in _walk_dirs(root, errors):
        msu_dir = path
        stem = path.name[: -len(".msu")]
        roms = _sibling_roms(msu_dir.parent, errors)
        rom = roms.get(stem.casefold())
        if rom is None:
            errors.append(f"{msu_dir}: no matching .sfc or .smc ROM found")
            continue
        try:
            real = os.path.realpath(rom)
        except OSError as error:
            errors.append(f"{rom}: {error}")
            continue
        if real in seen:
            continue
        seen.add(real)
        games.append({
            "name": rom.stem,
            "platform": "SNES",
            "genre": "",
            "path": str(rom),
            "source": "MSU-1",
            "msu1": True,
            "msu_path": str(msu_dir),
            "added_at": now,
        })
    return games


def _in_whdload_system_drawer(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return any(part.casefold() in WHDLOAD_SYSTEM_DRAWERS for part in parts)


def import_whdload(folder, errors=None):
    """Import Amiga WHDLoad installs (``.lha``/``.slave``/``.hdf``) recursively."""
    errors = _error_list(errors)
    root = _required_root(folder)
    games = []
    seen: set[str] = set()
    now = _imported_at()
    for path in _walk_files(root, errors):
        if path.suffix.casefold() not in WHDLOAD_EXTENSIONS:
            continue
        if _in_whdload_system_drawer(root, path):
            continue
        try:
            real = os.path.realpath(path)
        except OSError as error:
            errors.append(f"{path}: {error}")
            continue
        if real in seen:
            continue
        seen.add(real)
        games.append({
            "name": path.stem,
            "platform": "Amiga",
            "genre": "",
            "path": str(path),
            "source": "WHDLoad",
            "emulator_adapter_id": "amiga-whdload",
            "added_at": now,
        })
    return games
