"""Service-agnostic game-stat syncing through a mounted cloud folder."""

import json
from datetime import datetime
from pathlib import Path

STAT_FIELDS = ("play_count", "playtime_seconds", "last_played", "progress", "rating", "favorite")
PROGRESS = {"", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned"}


def nonnegative_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def game_key(game):
    if game.get("game_id"):
        return f"id:{game['game_id']}"
    return legacy_game_key(game)


def legacy_game_key(game):
    if game.get("steam_app_id"):
        return f"steam:{game['steam_app_id']}"
    if game.get("heroic_app_id"):
        return f"heroic:{game.get('source', '')}:{game['heroic_app_id']}"
    if game.get("lutris_id"):
        return f"lutris:{game['lutris_id']}"
    if game.get("rom_name"):
        return f"arcade:{game.get('source', '')}:{game['rom_name']}"
    return f"path:{Path(game.get('path', '')).expanduser()}"


def sync_statistics(state, folder, now=None):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Cloud sync folder does not exist: {folder}")
    target = folder / "openbox-statistics.json"
    try:
        remote = json.loads(target.read_text()) if target.is_file() else {}
    except (OSError, json.JSONDecodeError):
        remote = {}
    remote_games = remote.get("games", {}) if isinstance(remote, dict) and isinstance(remote.get("games", {}), dict) else {}
    last_sync = str(state.get("settings", {}).get("last_cloud_sync", ""))
    remote_is_newer = str(remote.get("generated_at", "")) > last_sync
    changed = 0
    for game in state["games"]:
        saved = remote_games.get(game_key(game), {})
        if not saved:
            saved = remote_games.get(legacy_game_key(game), {})
        if not isinstance(saved, dict):
            continue
        before = {field:game.get(field) for field in STAT_FIELDS}
        game["play_count"] = max(nonnegative_int(game.get("play_count")), nonnegative_int(saved.get("play_count")))
        game["playtime_seconds"] = max(nonnegative_int(game.get("playtime_seconds")), nonnegative_int(saved.get("playtime_seconds")))
        game["last_played"] = max(str(game.get("last_played", "")), str(saved.get("last_played", "")))
        if remote_is_newer:
            if str(saved.get("progress", "")) in PROGRESS:
                game["progress"] = str(saved.get("progress", ""))
            try:
                rating = float(saved.get("rating", game.get("rating", 0)))
                if 0 <= rating <= 5:
                    game["rating"] = rating
            except (TypeError, ValueError):
                pass
            if isinstance(saved.get("favorite"), bool):
                game["favorite"] = saved["favorite"]
        changed += before != {field:game.get(field) for field in STAT_FIELDS}
    timestamp = now or datetime.now().isoformat(timespec="seconds")
    payload = {
        "format": 1,
        "generated_at": timestamp,
        "games": {game_key(game):{field:game.get(field, 0 if field in {"play_count", "playtime_seconds", "rating"} else "") for field in STAT_FIELDS} for game in state["games"]},
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    temporary.replace(target)
    state.setdefault("settings", {})["last_cloud_sync"] = timestamp
    return {"path": str(target), "games": len(payload["games"]), "merged": changed, "synced_at": timestamp}
