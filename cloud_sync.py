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
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise CloudSyncError(
                f"Sync folder is busy (another sync is holding the lock): {target}"
            ) from error
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


# --- Full library sync (1.9.0) ---
# Extends cloud_sync beyond stats: publish/pull the entire game library via
# a mounted folder (Syncthing-style). Tombstones track deletions.
# ponytail: last-writer-wins conflict resolution by timestamp; no vector clocks.
# Upgrade path: add version vectors if multi-device conflicts become common.

LIBRARY_SYNC_FILE = "openbox-library.json"
TOMBSTONE_FIELDS = frozenset({"deleted_at", "device_id"})
TOMBSTONE_GC_DAYS = 90
TOMBSTONE_GC_SECONDS = TOMBSTONE_GC_DAYS * 86400
MASS_DELETE_THRESHOLD = 0.10


def _fields_differ(local_game, remote_game):
    """Field names whose values differ between local and remote copies."""
    keys = set(local_game) | set(remote_game)
    keys.discard("_sync_updated_at")
    return sorted(key for key in keys if local_game.get(key) != remote_game.get(key))


def _gc_tombstones(tombstones, now_ts):
    """Drop tombstones older than 90 days. Returns (kept, gc_count)."""
    kept = {}
    gc_count = 0
    for key, record in tombstones.items():
        if not isinstance(record, dict) or not record.get("deleted_at"):
            continue
        deleted_ts = _timestamp(record.get("deleted_at"))
        if deleted_ts and now_ts - deleted_ts > TOMBSTONE_GC_SECONDS:
            gc_count += 1
            continue
        kept[key] = record
    return kept, gc_count


def publish_library(state, folder, device_id="local", now=None):
    """Publish the full library to the mounted folder.

    Writes a JSON file with all games keyed by game_key, plus tombstones for
    games that were deleted since the last publish. Other devices pull this
    to merge into their local library.
    """
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Cloud sync folder does not exist: {folder}")
    target = folder / LIBRARY_SYNC_FILE
    timestamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
    with _sync_lock(target):
        # Load existing remote to preserve tombstones from other devices
        existing_tombstones = {}
        if target.is_file():
            try:
                remote = json.loads(target.read_text())
                if isinstance(remote, dict):
                    for key, record in (remote.get("tombstones") or {}).items():
                        if isinstance(record, dict) and record.get("deleted_at"):
                            existing_tombstones[key] = record
            except (OSError, json.JSONDecodeError):
                pass

        games_map = {}
        for game in state.get("games", []):
            if not isinstance(game, dict):
                continue
            key = game_key(game)
            if not key:
                continue
            games_map[key] = {
                "game": game,
                "updated_at": timestamp,
                "device_id": device_id,
            }

        # Carry forward tombstones for games not in local library
        tombstones = dict(existing_tombstones)
        for key in existing_tombstones:
            if key in games_map:
                # Game was re-added locally; clear tombstone
                tombstones.pop(key, None)

        # 90-day garbage collection for stale tombstones (ADR 0038)
        tombstones, tombstones_gc = _gc_tombstones(tombstones, _timestamp(timestamp))

        payload = {
            "format": 2,
            "generated_at": timestamp,
            "device_id": device_id,
            "media_synced": False,
            "games": games_map,
            "tombstones": tombstones,
        }
        atomic_write_text(target, json.dumps(payload, indent=2), mode=0o600)
    return {
        "path": str(target),
        "published_games": len(games_map),
        "tombstones": len(tombstones),
        "tombstones_gc": tombstones_gc,
        "media_synced": False,
        "synced_at": timestamp,
    }


def pull_library(state, folder, device_id="local", now=None, confirm=False):
    """Pull the remote library and merge into local state.

    Returns a merge result dict with added/updated/deleted/skipped counts and
    the mutated games list. Caller is responsible for persisting via
    transact_state.

    Conflict resolution: last-writer-wins by ``updated_at`` timestamp.
    Concurrent edits are reported in ``conflicts[]`` (game_key,
    local/remote updated_at, winner, fields_differ) without changing the
    winner. Tombstones delete local games that match the tombstone key.
    Pulls that would delete more than 10% of the local library require
    ``confirm=True``; without it the call returns ``needs_confirm`` with
    counts and leaves the library unmutated. Media is never synced
    (``media_synced`` stays False). Manual/shelf entries merge with
    ``path_usable`` so the receiving device renders a shelf badge instead
    of a missing-file error.
    """
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Cloud sync folder does not exist: {folder}")
    target = folder / LIBRARY_SYNC_FILE
    timestamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
    if not target.is_file():
        return {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "games": state.get("games", []), "synced_at": timestamp, "needs_confirm": False, "conflicts": []}

    with _sync_lock(target):
        try:
            remote = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise CloudRemoteInvalid("Remote library file is invalid or unreadable.") from error
        if not isinstance(remote, dict):
            raise CloudRemoteInvalid("Remote library file is invalid or unreadable.")

    remote_games = remote.get("games") or {}
    remote_tombstones = remote.get("tombstones") or {}
    if not isinstance(remote_games, dict):
        remote_games = {}
    if not isinstance(remote_tombstones, dict):
        remote_tombstones = {}

    local_games = list(state.get("games", []))
    local_by_key = {}
    for i, game in enumerate(local_games):
        if isinstance(game, dict):
            local_by_key[game_key(game)] = i

    added = 0
    updated = 0
    deleted = 0
    skipped = 0
    conflicts = []

    # Mass-delete gate: pulls deleting >10% of the local library need confirm.
    pending_deleted = sum(
        1 for tkey, tombstone in remote_tombstones.items()
        if isinstance(tombstone, dict) and tombstone.get("deleted_at") and tkey in local_by_key
    )
    if pending_deleted and local_games and pending_deleted / len(local_games) > MASS_DELETE_THRESHOLD and not confirm:
        return {
            "added": 0,
            "updated": 0,
            "deleted": pending_deleted,
            "skipped": 0,
            "games": list(state.get("games", [])),
            "synced_at": timestamp,
            "needs_confirm": True,
            "local_count": len(local_games),
            "conflicts": [],
        }

    # Apply tombstones: delete local games that are tombstoned remotely
    for tkey, tombstone in remote_tombstones.items():
        if not isinstance(tombstone, dict) or not tombstone.get("deleted_at"):
            continue
        if tkey in local_by_key:
            idx = local_by_key[tkey]
            if idx < len(local_games):
                local_games.pop(idx)
                deleted += 1
                # Rebuild index after deletion
                local_by_key = {}
                for i, game in enumerate(local_games):
                    if isinstance(game, dict):
                        local_by_key[game_key(game)] = i

    # Merge remote games
    for rkey, record in remote_games.items():
        if not isinstance(record, dict) or not isinstance(record.get("game"), dict):
            skipped += 1
            continue
        remote_game = record["game"]
        remote_updated = _timestamp(record.get("updated_at", ""))
        if rkey in local_by_key:
            idx = local_by_key[rkey]
            local_game = local_games[idx]
            local_updated = _timestamp(local_game.get("_sync_updated_at", "0"))
            differ = _fields_differ(local_game, remote_game)
            if differ:
                conflicts.append({
                    "game_key": rkey,
                    "local_updated_at": local_game.get("_sync_updated_at", ""),
                    "remote_updated_at": record.get("updated_at", ""),
                    "winner": "remote" if remote_updated > local_updated else "local",
                    "fields_differ": differ,
                })
            if remote_updated > local_updated:
                if remote_game.get("manual_entry"):
                    remote_game["path_usable"] = True
                remote_game["_sync_updated_at"] = record.get("updated_at", "")
                local_games[idx] = remote_game
                updated += 1
            else:
                skipped += 1
        else:
            if remote_game.get("manual_entry"):
                remote_game["path_usable"] = True
            remote_game["_sync_updated_at"] = record.get("updated_at", "")
            local_games.append(remote_game)
            local_by_key[rkey] = len(local_games) - 1
            added += 1

    return {
        "added": added,
        "updated": updated,
        "deleted": deleted,
        "skipped": skipped,
        "games": local_games,
        "synced_at": timestamp,
        "needs_confirm": False,
        "conflicts": conflicts,
    }
