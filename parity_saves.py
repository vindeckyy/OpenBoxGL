"""Save retention, scan helpers, and extra emulator save locations."""

from __future__ import annotations

from pathlib import Path

from saves import discover_save_paths, list_backups


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
                candidates.append({"path": str(root), "label": "Dolphin Wii/WiiWare", "shared": True})
    if platform in {"Sega Saturn", "Saturn"}:
        for root in (
            home / ".config/retroarch/saves",
            home / ".var/app/org.libretro.RetroArch/config/retroarch/saves",
            home / ".mednafen",
        ):
            if root.exists():
                candidates.append({"path": str(root), "label": "Saturn / Mednafen saves", "shared": True})
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


def scan_all_saves(games, home=None):
    home = Path(home or Path.home())
    found = {}
    for index, game in enumerate(games):
        paths = []
        for item in discover_save_paths(game, home=home):
            paths.append(item["path"])
        for item in extra_save_candidates(game, home=home):
            if item["path"] not in paths:
                paths.append(item["path"])
        for item in extra_save_candidates(game, home=home):
            if item["path"] not in paths:
                paths.append(item["path"])
        existing = [path for path in paths if Path(path).exists()]
        if existing:
            found[index] = existing
    return found


def games_with_saves(games, home=None):
    scanned = scan_all_saves(games, home=home)
    indices = set(scanned)
    for index, game in enumerate(games):
        if any(Path(path).expanduser().exists() for path in game.get("save_paths", []) if str(path).strip()):
            indices.add(index)
    return sorted(indices)
