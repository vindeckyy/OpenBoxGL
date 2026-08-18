"""Canonical identity normalization for imported games."""

from __future__ import annotations

import os
import re
from pathlib import Path


def normalize_path_identity(path: str) -> str:
    """Canonicalize a filesystem path for identity comparison.
    
    - Expand user (~)
    - Resolve to absolute
    - Normalize case via os.path.normcase
    - Normalize path separators
    - Remove trailing separators
    - Resolve symlinks consistently
    """
    if not path:
        return ""
    try:
        p = Path(path).expanduser().resolve()
        norm = os.path.normcase(str(p))
    except Exception:
        # Fallback if resolve fails
        norm = os.path.normcase(os.path.normpath(os.path.expanduser(path)))
    
    return norm.rstrip(os.sep)


def normalize_rom_name(filename: str) -> str:
    """Normalize a ROM filename for identity.
    
    - Strip common suffixes (.zip, .7z, .iso, .bin, .cue, .chd, etc.)
    - Remove region/revision tags like (USA), (Rev 1), [!], etc.
    - Lowercase
    - Strip whitespace
    """
    if not filename:
        return ""
    
    name = str(filename)
    
    # Strip common extensions at the end
    extensions = r"\.(zip|7z|rar|tar|gz|iso|bin|cue|chd|nes|sfc|smc|gba|nds|n64|z64|gcm|wbfs|pbp|pkg|xiso|xbe|mdf|mds|img|ccd|sub)$"
    name = re.sub(extensions, "", name, flags=re.IGNORECASE)
    
    # Strip region/revision tags like (USA), [!], (En,Fr,De)
    name = re.sub(r"\(.*?\)|\[.*?\]", "", name)
    
    # Lowercase, strip whitespace, and normalize multiple spaces to single
    name = name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"\s+\.", ".", name)
    
    return name


def normalize_identity(game: dict) -> str | None:
    """Return a canonical source_identity string for a game record.
    
    Rules:
    - Steam: 'steam:<app_id>'
    - Heroic: 'heroic:<source>:<app_id>'
    - Lutris: 'lutris:<id>'
    - Gameyfin: 'gameyfin:<id>'
    - Arcade/ROM: '<source>:<normalized_rom_name>'
    - Generic file: 'path:<canonical_absolute_path>'
    - Returns None if no identity can be determined
    """
    if not game:
        return None

    if game.get("steam_app_id"):
        return f"steam:{game['steam_app_id']}"
    
    if game.get("heroic_app_id"):
        source = str(game.get("source", "")).strip()
        app_id = str(game["heroic_app_id"]).strip()
        return f"heroic:{source}:{app_id}"
        
    if game.get("lutris_id"):
        return f"lutris:{game['lutris_id']}"
        
    if game.get("gameyfin_id"):
        return f"gameyfin:{game['gameyfin_id']}"
        
    rom_name = game.get("rom_name")
    if rom_name:
        source = str(game.get("source") or "arcade").strip()
        norm_rom = normalize_rom_name(rom_name)
        return f"{source}:{norm_rom}"
        
    if game.get("path"):
        norm_path = normalize_path_identity(game["path"])
        if norm_path:
            return f"path:{norm_path}"
            
    return None


def detect_duplicate_identities(games: list[dict]) -> list[dict]:
    """Find games with duplicate canonical identities.
    
    Returns list of {identity: str, games: [game_id, ...]} for duplicates.
    Does NOT delete or merge — only reports.
    """
    identities = {}
    for game in games:
        ident = normalize_identity(game)
        if ident:
            game_id = game.get("id")
            if game_id:
                identities.setdefault(ident, []).append(game_id)
            
    duplicates = []
    for ident, gids in identities.items():
        if len(gids) > 1:
            duplicates.append({"identity": ident, "games": gids})
            
    return duplicates


def backfill_source_identity(games: list[dict]) -> int:
    """Add source_identity field to games that lack it.
    
    Returns count of games updated.
    Generic files that are moved remain a reported identity conflict
    until the user explicitly merges or rebinds.
    """
    count = 0
    for game in games:
        if "source_identity" not in game:
            ident = normalize_identity(game)
            if ident:
                game["source_identity"] = ident
                count += 1
    return count
