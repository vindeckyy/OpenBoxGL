"""Library-level operations shared by the OpenBox interfaces."""

import re
from datetime import datetime

PROGRESS = {"", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned"}
MEDIA_FIELDS = ("cover", "background", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "video", "music")


def related_game_ids(games, selected, limit=8):
    """Return related game IDs scored from local metadata only."""
    base = games[selected]
    base_genres = set(re.findall(r"\w+", str(base.get("genre", "")).lower()))
    ranked = []
    for index, game in enumerate(games):
        if index == selected or game.get("hidden"):
            continue
        genres = set(re.findall(r"\w+", str(game.get("genre", "")).lower()))
        score = 2 * len(base_genres & genres)
        score += 8 * bool(base.get("series") and base.get("series") == game.get("series"))
        score += 5 * bool(base.get("collection") and base.get("collection") == game.get("collection"))
        score += 3 * bool(base.get("developer") and base.get("developer") == game.get("developer"))
        score += 2 * bool(base.get("platform") and base.get("platform") == game.get("platform"))
        score += bool(base.get("publisher") and base.get("publisher") == game.get("publisher"))
        if score:
            ranked.append((-score, str(game.get("sort_title") or game.get("name", "")).casefold(), index))
    return [index for _, _, index in sorted(ranked)[:limit]]


def bulk_update(games, ids, changes):
    allowed = {"platform", "genre", "progress", "rating", "favorite", "hidden", "esrb", "custom_fields"}
    if not isinstance(ids, list) or not ids:
        raise ValueError("Select at least one game.")
    if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
        raise ValueError("No valid bulk changes were supplied.")
    clean = {}
    for field, value in changes.items():
        if field in {"favorite", "hidden"}:
            if not isinstance(value, bool):
                raise ValueError(f"{field.title()} must be true or false.")
            clean[field] = value
        elif field == "progress":
            if str(value) not in PROGRESS:
                raise ValueError("Unknown progress value.")
            clean[field] = str(value)
        elif field == "rating":
            rating = float(value)
            if not 0 <= rating <= 5:
                raise ValueError("Rating must be between 0 and 5.")
            clean[field] = rating
        elif field == "esrb":
            clean[field] = str(value).strip()
        elif field == "custom_fields":
            if not isinstance(value, dict):
                raise ValueError("Custom fields must be an object.")
            clean[field] = {str(key).strip(): str(val).strip() for key, val in value.items() if str(key).strip()}
        else:
            clean[field] = str(value).strip()
    stable_indexes = {}
    for index, game in enumerate(games):
        if game.get("game_id"):
            stable_indexes[str(game["game_id"])] = index
        aliases = game.get("legacy_game_ids", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if str(alias).strip():
                    stable_indexes[str(alias)] = index
    selected = sorted({
        stable_indexes[str(value)] if str(value) in stable_indexes else int(value)
        for value in ids
    })
    if selected[0] < 0 or selected[-1] >= len(games):
        raise IndexError("A selected game no longer exists.")
    for index in selected:
        patch = dict(clean)
        if "custom_fields" in patch:
            merged = games[index].get("custom_fields", {})
            if not isinstance(merged, dict):
                merged = {}
            merged.update(patch.pop("custom_fields"))
            games[index]["custom_fields"] = merged
        games[index].update(patch)
    return len(selected)


def apply_progress_automation(game, settings, now=None):
    """Apply LaunchBox-style progress automation after a session ends."""
    if not settings.get("progress_automation_enabled"):
        return
    now = now or datetime.now()
    play_minutes = int(settings.get("progress_automation_play_minutes", 0) or 0)
    idle_days = int(settings.get("progress_automation_idle_days", 0) or 0)
    if play_minutes and game.get("playtime_seconds", 0) >= play_minutes * 60:
        if not game.get("progress") or game["progress"] == "Paused":
            game["progress"] = "Playing"
    if idle_days and game.get("last_played"):
        try:
            last = datetime.fromisoformat(str(game["last_played"]))
            if (now - last).days >= idle_days and game.get("progress") == "Playing":
                game["progress"] = "Paused"
        except ValueError:
            pass


def game_media_paths(game):
    paths = []
    for field in MEDIA_FIELDS:
        path = str(game.get(field, "")).strip()
        if path:
            paths.append(path)
    paths.extend(str(path).strip() for path in game.get("screenshots", []) if str(path).strip())
    return paths
