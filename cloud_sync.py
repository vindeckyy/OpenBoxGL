"""Service-agnostic game-stat syncing through a mounted cloud folder."""

import json
import fcntl
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

from backend_io import atomic_write_text

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


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


@contextmanager
def _sync_lock(target):
    lock_path = Path(target).with_name(f".{Path(target).name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def sync_statistics(state, folder, now=None):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Cloud sync folder does not exist: {folder}")
    target = folder / "openbox-statistics.json"
    with _sync_lock(target):
        try:
            remote = json.loads(target.read_text()) if target.is_file() else {}
        except (OSError, json.JSONDecodeError):
            remote = {}
        remote_games = remote.get("games", {}) if isinstance(remote, dict) and isinstance(remote.get("games", {}), dict) else {}
        remote_generated = _timestamp(remote.get("generated_at", ""))
        last_sync = _timestamp(state.get("settings", {}).get("last_cloud_sync", ""))
        remote_newer_overall = remote_generated > last_sync
        merged = {}
        changed = 0
        for game in state["games"]:
            key = game_key(game)
            saved = remote_games.get(key, {})
            if not isinstance(saved, dict):
                saved = {}
            remote_key = key
            if not saved:
                legacy = legacy_game_key(game)
                saved = remote_games.get(legacy, {})
                if isinstance(saved, dict):
                    remote_key = legacy
            if not isinstance(saved, dict):
                saved = {}
            local_played = _timestamp(game.get("last_played"))
            remote_played = _timestamp(saved.get("last_played"))
            if local_played or remote_played:
                # The side with the newer last_played is authoritative.
                remote_wins = remote_played > local_played
            else:
                # No play timestamps on either side: defer to file freshness.
                remote_wins = remote_newer_overall
            before = {field: game.get(field) for field in STAT_FIELDS}
            game["play_count"] = max(nonnegative_int(game.get("play_count")), nonnegative_int(saved.get("play_count")))
            game["playtime_seconds"] = max(nonnegative_int(game.get("playtime_seconds")), nonnegative_int(saved.get("playtime_seconds")))
            game["last_played"] = max(str(game.get("last_played", "")), str(saved.get("last_played", "")))
            if remote_wins:
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
            elif saved:
                # Local changes win; keep newer per-field values from the remote.
                if remote_played > local_played:
                    for field in ("progress", "rating", "favorite"):
                        if saved.get(field) is not None and str(saved.get(field)) != "":
                            game[field] = saved[field]
            changed += before != {field: game.get(field) for field in STAT_FIELDS}
            merged[key] = {
                field: game.get(field, 0 if field in {"play_count", "playtime_seconds", "rating"} else "")
                for field in STAT_FIELDS
            }
            if remote_key != key:
                merged.pop(remote_key, None)
        timestamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
        if remote_generated and last_sync >= remote_generated:
            # Local state is newer; bump the remote timestamp to now.
            generated_at = timestamp
        else:
            # Keep the remote timestamp when newer or first sync, so the next sync can still compare.
            generated_at = remote.get("generated_at", timestamp)
        payload = {
            "format": 1,
            "generated_at": generated_at,
            "games": merged,
        }
        atomic_write_text(target, json.dumps(payload, indent=2), mode=0o600)
    state.setdefault("settings", {})["last_cloud_sync"] = timestamp
    return {"path": str(target), "games": len(payload["games"]), "merged": changed, "synced_at": timestamp}
