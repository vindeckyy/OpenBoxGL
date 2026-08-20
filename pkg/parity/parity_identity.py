"""Canonical identity normalization for imported games."""

from __future__ import annotations

import os
import re
from pathlib import Path

PC_STORE_SOURCES = {
    "steam", "epic", "gog", "amazon", "ea", "ubisoft", "xbox", "lutris",
    "gameyfin", "faugus",
}
PC_PLATFORMS = {"", "pc", "windows", "linux", "mac", "macos"}


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
    return re.sub(r"\s+\.", ".", name)


def normalize_title_identity(name: str) -> str:
    if not name:
        return ""
    value = str(name).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def source_identities(game: dict) -> list[str]:
    if not game:
        return []
    identities = []
    for identity in game.get("source_identities", []):
        value = str(identity).strip()
        if value and value not in identities:
            identities.append(value)
    source_identity = str(game.get("source_identity") or "").strip()
    if source_identity and source_identity not in identities:
        identities.append(source_identity)
    if game.get("steam_app_id"):
        identities.append(f"steam:{game['steam_app_id']}")
    if game.get("heroic_app_id"):
        source = str(game.get("heroic_source") or game.get("source") or "").strip()
        if source:
            identities.append(f"heroic:{source}:{game['heroic_app_id']}")
    if game.get("lutris_id"):
        identities.append(f"lutris:{game['lutris_id']}")
    if game.get("gameyfin_id"):
        identities.append(f"gameyfin:{game['gameyfin_id']}")
    if game.get("faugus_id"):
        identities.append(f"faugus:{game['faugus_id']}")
    if game.get("rom_name"):
        source = str(game.get("source") or "arcade").strip()
        identities.append(f"{source}:{normalize_rom_name(game['rom_name'])}")
    if not identities and game.get("path"):
        norm_path = normalize_path_identity(game["path"])
        if norm_path:
            identities.append(f"path:{norm_path}")
    return list(dict.fromkeys(identities))


def source_family(game: dict) -> str:
    if not game:
        return ""
    if game.get("steam_app_id"):
        return "steam"
    if game.get("heroic_app_id"):
        source = str(game.get("heroic_source") or game.get("source") or "").strip().casefold()
        return f"heroic:{source}" if source else "heroic"
    if game.get("lutris_id"):
        return "lutris"
    if game.get("gameyfin_id"):
        return "gameyfin"
    if game.get("faugus_id"):
        return "faugus"
    return str(game.get("source") or game.get("collection") or "").strip().casefold()


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

    source_identity = str(game.get("source_identity") or "").strip()
    if source_identity:
        return source_identity

    if game.get("source_identities"):
        identities = source_identities(game)
        if identities:
            return identities[0]

    if game.get("steam_app_id"):
        return f"steam:{game['steam_app_id']}"
    
    if game.get("heroic_app_id"):
        source = str(game.get("heroic_source") or game.get("source") or "").strip()
        app_id = str(game["heroic_app_id"]).strip()
        return f"heroic:{source}:{app_id}"
        
    if game.get("lutris_id"):
        return f"lutris:{game['lutris_id']}"
        
    if game.get("gameyfin_id"):
        return f"gameyfin:{game['gameyfin_id']}"

    if game.get("faugus_id"):
        return f"faugus:{game['faugus_id']}"
        
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


def cross_source_identity(game: dict) -> str | None:
    if not game:
        return None
    source = str(game.get("source") or game.get("collection") or "").strip().casefold()
    platform = str(game.get("platform") or "").strip().casefold()
    has_store_id = any(game.get(field) for field in (
        "steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id", "faugus_id",
    ))
    if (source not in PC_STORE_SOURCES and not has_store_id) or platform not in PC_PLATFORMS:
        return None
    title = normalize_title_identity(game.get("name", ""))
    return f"pc-title:{title}" if title else None


def detect_duplicate_identities(games: list[dict], *, include_cross_source: bool = False) -> list[dict]:
    """Find games with duplicate canonical identities.
    
    Returns list of {identity: str, games: [game_id, ...]} for duplicates.
    Does NOT delete or merge — only reports.
    """
    identities = {}
    cross_identities = {}
    for game in games:
        game_id = game.get("game_id") or game.get("id")
        if not game_id:
            continue
        candidates = source_identities(game)
        for ident in dict.fromkeys(candidates):
            identities.setdefault(ident, []).append(game_id)
        if include_cross_source:
            cross_identity = cross_source_identity(game)
            family = source_family(game)
            if cross_identity and family:
                bucket = cross_identities.setdefault(cross_identity, {"games": [], "families": set()})
                bucket["games"].append(game_id)
                bucket["families"].add(family)
            
    duplicates = []
    for ident, gids in identities.items():
        if len(gids) > 1:
            duplicates.append({"identity": ident, "games": gids})
    for ident, bucket in cross_identities.items():
        if len(bucket["games"]) > 1 and len(bucket["families"]) > 1:
            duplicates.append({"identity": ident, "games": bucket["games"]})
            
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
