"""Library export (1.8.0): game rows as JSON or CSV, redaction-safe by construction.

Exports contain only game-table fields — never settings, credentials, or
webhooks — so they are safe to share by default. Media path fields are
optional (``include_media_paths``). Scope is server-side and deterministic:
the whole library, one platform, or one playlist's members.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path

EXPORT_FORMATS = ("json", "csv")
EXPORT_SCOPES = ("all", "platform", "playlist")
EXPORT_DIR_NAME = "exports"
EXPORT_PREFIX = "openbox-library-"
EXPORT_KEEP = 10
EXPORT_NAME_RE = re.compile(rf"^{EXPORT_PREFIX}[0-9]{{8}}-[0-9]{{6}}(?:-[0-9]+)?\.(json|csv)$")

# Ordered, shareable game fields. Keys mirror the FIELDS schema; anything
# secret-adjacent (credentials live in settings, not games) is absent.
EXPORT_GAME_FIELDS = (
    "game_id", "name", "sort_title", "alternate_names",
    "platform", "genre", "year", "developer", "publisher", "series",
    "region", "esrb", "max_players", "description", "notes", "wikipedia_url",
    "progress", "rating", "play_count", "playtime_seconds", "last_played",
    "favorite", "hidden", "broken", "portable", "installed", "added_at",
    "source", "steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id",
    "igdb_id", "ra_game_id", "launchbox_db_id", "install_dir",
    "disc_count", "rom_name", "clone_of", "set_type",
)
EXPORT_MEDIA_FIELDS = (
    "cover", "background", "clear_logo", "fanart", "banner", "icon",
    "box_back", "box_spine", "box_3d", "title_screen", "cart_front",
    "cart_back", "disc", "advertisement", "manual",
    "video", "video_snap", "video_theme", "video_trailer", "video_recording",
    "music", "screenshots",
)


def build_export_games(state: dict, scope: str = "all", scope_name: str = "") -> list[dict]:
    """Return the games selected by *scope*: all, one platform, or one playlist."""
    if not isinstance(state, dict):
        return []
    games = state.get("games", [])
    if not isinstance(games, list):
        return []
    if scope not in EXPORT_SCOPES:
        raise ValueError("Unknown export scope.")
    if scope == "all":
        return [game for game in games if isinstance(game, dict)]
    key = str(scope_name or "").strip()
    if not key:
        raise ValueError(f"Export scope {scope} requires a name.")
    if scope == "platform":
        return [game for game in games if isinstance(game, dict) and game.get("platform") == key]
    playlist = next((item for item in state.get("playlists", []) or [] if isinstance(item, dict) and item.get("name") == key), None)
    if playlist is None:
        raise ValueError(f"Playlist not found: {key}")
    members = set(playlist.get("members", []) or [])
    return [game for game in games if isinstance(game, dict) and (game.get("game_id") in members or game.get("id") in members)]


def export_row(game: dict, include_media_paths: bool = False) -> dict:
    """Project one game onto the export field set."""
    row = {}
    for field in EXPORT_GAME_FIELDS:
        value = game.get(field)
        if isinstance(value, list):
            value = ";".join(str(item) for item in value)
        row[field] = value if value is not None else ""
    if include_media_paths:
        for field in EXPORT_MEDIA_FIELDS:
            value = game.get(field)
            if isinstance(value, list):
                value = ";".join(str(item) for item in value)
            row[field] = value if value is not None else ""
    return row


def export_rows(games: list[dict], include_media_paths: bool = False) -> list[dict]:
    return [export_row(game, include_media_paths) for game in games if isinstance(game, dict)]


def write_export(data_dir: Path, rows: list[dict], fmt: str = "json", include_media_paths: bool = False) -> Path:
    """Write rows to exports/openbox-library-<stamp>.<fmt> and prune old exports."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError("Unknown export format.")
    export_dir = Path(data_dir) / EXPORT_DIR_NAME
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = export_dir / f"{EXPORT_PREFIX}{stamp}.{fmt}"
    counter = 1
    while path.exists():
        counter += 1
        path = export_dir / f"{EXPORT_PREFIX}{stamp}-{counter}.{fmt}"
    if fmt == "json":
        payload = {
            "application": "OpenBox",
            "kind": "library-export",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(rows),
            "include_media_paths": bool(include_media_paths),
            "games": rows,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        buffer = io.StringIO()
        fields = list(EXPORT_GAME_FIELDS) + (list(EXPORT_MEDIA_FIELDS) if include_media_paths else [])
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8")
    prune_exports(export_dir, keep=EXPORT_KEEP)
    return path


def prune_exports(export_dir: Path, keep: int = EXPORT_KEEP) -> None:
    """Keep only the newest *keep* export files."""
    export_dir = Path(export_dir)
    if not export_dir.is_dir():
        return
    def sort_key(item: Path):
        try:
            return (item.stat().st_mtime, item.name)
        except OSError:
            return (0.0, item.name)

    files = sorted(
        (item for item in export_dir.iterdir() if item.is_file() and EXPORT_NAME_RE.match(item.name)),
        key=sort_key,
    )
    for stale in files[:-keep] if keep > 0 else files:
        try:
            stale.unlink()
        except OSError:
            pass


def list_exports(data_dir: Path) -> list[dict]:
    """Return metadata for existing export files, newest first."""
    export_dir = Path(data_dir) / EXPORT_DIR_NAME
    if not export_dir.is_dir():
        return []
    items = []
    for item in export_dir.iterdir():
        if not item.is_file() or not EXPORT_NAME_RE.match(item.name):
            continue
        try:
            stat_result = item.stat()
        except OSError:
            continue
        items.append({"name": item.name, "bytes": stat_result.st_size, "modified": int(stat_result.st_mtime)})
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


def approved_export_file(data_dir: Path, name: str) -> Path | None:
    """Return the export path for *name* if it is a real, contained export file."""
    candidate = str(name or "").strip()
    if not EXPORT_NAME_RE.match(candidate):
        return None
    path = Path(data_dir) / EXPORT_DIR_NAME / candidate
    if path.parent.resolve() != (Path(data_dir) / EXPORT_DIR_NAME).resolve():
        return None
    if not path.is_file():
        return None
    return path
