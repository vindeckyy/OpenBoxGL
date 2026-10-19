"""Save retention, scan helpers, and extra emulator save locations."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from saves import discover_save_paths, list_backups


_SAVE_CACHE_LOCK = threading.RLock()
_SAVE_CACHE = {"at": 0.0, "signature": None, "indices": []}


def extra_save_candidates(game, home=None):
    home = Path(home or Path.home())
    platform = str(game.get("platform", ""))
    candidates = []
    if platform in {"WiiWare", "Wii"}:
        for root in (
            home / ".local/share/dolphin-emu/Wii/title",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title",
        ):
            if root.exists():
                candidates.append({
                    "path": str(root),
                    "label": "Dolphin Wii/WiiWare",
                    "shared": True,
                    "emulator_id": "dolphin",
                    "core": "",
                    "slot": "",
                })
    if platform in {"Sega Saturn", "Saturn"}:
        for root in (
            home / ".config/retroarch/saves",
            home / ".var/app/org.libretro.RetroArch/config/retroarch/saves",
            home / ".mednafen",
        ):
            if root.exists():
                candidates.append({
                    "path": str(root),
                    "label": "Saturn / Mednafen saves",
                    "shared": True,
                    "emulator_id": "mednafen" if Path(root).name == ".mednafen" else "retroarch",
                    "core": "",
                    "slot": "",
                })
    return candidates


def enforce_backup_limit(game, root, max_backups):
    max_backups = int(max_backups or 0)
    if max_backups <= 0:
        return 0
    backups = list_backups(game, root)
    removed = 0
    for archive in backups[max_backups:]:
        try:
            archive.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def _source_metadata(item):
    return {
        key: str(item.get(key) or "")
        for key in ("emulator_id", "core", "slot")
    }


def scan_all_saves(games, home=None):
    """Discover existing save paths and record their per-path provenance.

    ``game["save_sources"]`` maps each existing discovered path to the
    emulator (and, when known, core/slot) that owns it.  The returned mapping
    keeps the historical ``{game_index: [path, ...]}`` shape so callers that
    only write ``save_paths`` keep working.
    """
    home = Path(home or Path.home())
    found = {}
    for index, game in enumerate(games):
        paths = []
        sources = {}
        for item in discover_save_paths(game, home=home):
            path = str(item.get("path") or "")
            if not path:
                continue
            if path not in paths:
                paths.append(path)
            sources[path] = _source_metadata(item)
        for item in extra_save_candidates(game, home=home):
            path = str(item.get("path") or "")
            if not path:
                continue
            if path not in paths:
                paths.append(path)
            sources[path] = _source_metadata(item)
        existing = [path for path in paths if Path(path).exists()]
        if existing:
            found[index] = existing
            game["save_sources"] = {
                path: sources[path] for path in existing if path in sources
            }
    return found


def games_with_saves(games, home=None):
    home_key = str(Path(home or Path.home()).expanduser())
    signature = tuple(
        (
            str(game.get("game_id") or index),
            str(game.get("path") or ""),
            tuple(str(path) for path in game.get("save_paths", []) if str(path).strip()),
        )
        for index, game in enumerate(games)
    ) + (home_key,)
    with _SAVE_CACHE_LOCK:
        if _SAVE_CACHE["signature"] == signature and time.monotonic() - _SAVE_CACHE["at"] < 2:
            return list(_SAVE_CACHE["indices"])
    scanned = scan_all_saves(games, home=home)
    indices = set(scanned)
    for index, game in enumerate(games):
        if any(Path(path).expanduser().exists() for path in game.get("save_paths", []) if str(path).strip()):
            indices.add(index)
    result = sorted(indices)
    with _SAVE_CACHE_LOCK:
        _SAVE_CACHE.update({"at": time.monotonic(), "signature": signature, "indices": result})
    return result
