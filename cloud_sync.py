"""Service-agnostic game-stat syncing through a mounted cloud folder."""

import json
import fcntl
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

from backend_io import atomic_write_text
from notifications import record_cloud_sync_outcome

STAT_FIELDS = ("play_count", "playtime_seconds", "last_played", "progress", "rating", "favorite")
PROGRESS = {"", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned"}


class CloudSyncError(Exception):
    """Base error for cloud statistics sync."""

    code = "CLOUD_SYNC_ERROR"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.message = str(message)
        self.code = code or self.__class__.code


class CloudRemoteInvalid(CloudSyncError):
    code = "CLOUD_REMOTE_INVALID"


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


def _load_remote_state(target):
    if not target.is_file():
        return {}, {}
    try:
        remote = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CloudRemoteInvalid("Remote cloud statistics file is invalid or unreadable.") from error
    if not isinstance(remote, dict):
        raise CloudRemoteInvalid("Remote cloud statistics file is invalid or unreadable.")
    remote_games = remote.get("games", {})
    if not isinstance(remote_games, dict):
        raise CloudRemoteInvalid("Remote cloud statistics file is invalid or unreadable.")
    return remote, remote_games


def _resolve_saved_record(remote_games, game, key):
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
    return saved, remote_key


def _local_progress_missing(game):
    progress = str(game.get("progress", ""))
    return progress == "" or progress not in PROGRESS


def _local_rating_missing(game):
    try:
        return float(game.get("rating", 0)) == 0
    except (TypeError, ValueError):
        return True


def _local_favorite_missing(game):
    return "favorite" not in game


def _pick_last_played(local_value, remote_value):
    local_text = str(local_value or "")
    remote_text = str(remote_value or "")
    local_ts = _timestamp(local_text)
    remote_ts = _timestamp(remote_text)
    if local_ts > remote_ts:
        return local_text
    if remote_ts > local_ts:
        return remote_text
    return remote_text or local_text


def _fill_progress(game, saved):
    if not _local_progress_missing(game):
        return
    progress = str(saved.get("progress", ""))
    if progress in PROGRESS and progress:
        game["progress"] = progress


def _fill_rating(game, saved):
    if not _local_rating_missing(game):
        return
    try:
        rating = float(saved.get("rating", 0))
    except (TypeError, ValueError):
        return
    if 0 <= rating <= 5:
        game["rating"] = rating


def _fill_favorite(game, saved):
    if not _local_favorite_missing(game):
        return
    if isinstance(saved.get("favorite"), bool):
        game["favorite"] = saved["favorite"]


def _merge_game_stats(game, saved):
    before = {field: game.get(field) for field in STAT_FIELDS}
    game["play_count"] = max(nonnegative_int(game.get("play_count")), nonnegative_int(saved.get("play_count")))
    game["playtime_seconds"] = max(
        nonnegative_int(game.get("playtime_seconds")),
        nonnegative_int(saved.get("playtime_seconds")),
    )
    game["last_played"] = _pick_last_played(game.get("last_played", ""), saved.get("last_played", ""))
    if saved:
        _fill_progress(game, saved)
        _fill_rating(game, saved)
        _fill_favorite(game, saved)
    changed = before != {field: game.get(field) for field in STAT_FIELDS}
    merged = {
        field: game.get(field, 0 if field in {"play_count", "playtime_seconds", "rating"} else "")
        for field in STAT_FIELDS
    }
    return merged, changed


def _resolve_generated_at(remote, remote_generated, last_sync, timestamp):
    if remote_generated and last_sync >= remote_generated:
        generated_at = timestamp
    else:
        generated_at = remote.get("generated_at", timestamp)
    return generated_at


def sync_statistics(state, folder, now=None):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Cloud sync folder does not exist: {folder}")
    target = folder / "openbox-statistics.json"
    timestamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with _sync_lock(target):
            remote, remote_games = _load_remote_state(target)
            remote_generated = _timestamp(remote.get("generated_at", ""))
            last_sync = _timestamp(state.get("settings", {}).get("last_cloud_sync", ""))
            merged = {}
            changed = 0
            for game in state["games"]:
                key = game_key(game)
                saved, remote_key = _resolve_saved_record(remote_games, game, key)
                merged_record, record_changed = _merge_game_stats(game, saved)
                changed += record_changed
                merged[key] = merged_record
                if remote_key != key:
                    merged.pop(remote_key, None)
            generated_at = _resolve_generated_at(remote, remote_generated, last_sync, timestamp)
            payload = {
                "format": 1,
                "generated_at": generated_at,
                "games": merged,
            }
            atomic_write_text(target, json.dumps(payload, indent=2), mode=0o600)
    except CloudSyncError as error:
        record_cloud_sync_outcome(state, success=False, body=error.message, now=timestamp)
        raise
    state.setdefault("settings", {})["last_cloud_sync"] = timestamp
    record_cloud_sync_outcome(
        state,
        success=True,
        body=f"Synced {len(state.get('games', []))} game(s).",
        now=timestamp,
    )
    return {"path": str(target), "games": len(payload["games"]), "merged": changed, "synced_at": timestamp}
