"""Library-level operations shared by the OpenBox interfaces."""

import re
from datetime import datetime

PROGRESS = {"", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned"}
MEDIA_FIELDS = ("cover", "background", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual", "video", "music")
MAX_TAGS = 50
MAX_TAG_LENGTH = 64


def normalize_tags(value):
    """Canonicalize tags: trimmed strings deduped by casefold; over-long values and non-lists raise ValueError."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Tags must be a list.")
    clean = []
    seen = set()
    for raw in value:
        label = " ".join(str(raw).split())
        if not label:
            continue
        if len(label) > MAX_TAG_LENGTH:
            raise ValueError(f"Tags are limited to {MAX_TAG_LENGTH} characters.")
        folded = label.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        clean.append(label)
        if len(clean) >= MAX_TAGS:
            break
    return clean


def apply_tag_changes(game, *, replace=None, add=None, remove=None):
    """Apply replace/add/remove tag operations to a game dict; returns True when the stored value changed."""
    current = game.get("tags")
    if not isinstance(current, list):
        current = []
    result = list(current)
    if replace is not None:
        result = normalize_tags(replace)
    if add is not None:
        additions = normalize_tags(add)
        existing = {label.casefold() for label in result}
        for label in additions:
            if label.casefold() not in existing:
                result.append(label)
                existing.add(label.casefold())
    if remove is not None:
        removals = normalize_tags(remove)
        drop = {label.casefold() for label in removals}
        result = [label for label in result if label.casefold() not in drop]
    result = result[:MAX_TAGS]
    if result == current:
        return False
    game["tags"] = result
    return True


def tag_counts(games):
    """Return ``[{"tag": str, "count": int}, ...]`` for visible games, sorted by count then spelling."""
    counts = {}
    for game in games:
        if not isinstance(game, dict) or game.get("hidden"):
            continue
        for label in game.get("tags", []):
            if not isinstance(label, str) or not label.strip():
                continue
            key = label.strip().casefold()
            if key:
                counts.setdefault(key, {"tag": label.strip(), "count": 0})["count"] += 1
    return sorted(counts.values(), key=lambda item: (-item["count"], item["tag"].casefold()))


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
    allowed = {"platform", "genre", "progress", "rating", "favorite", "hidden", "esrb", "custom_fields", "tags", "tags_add", "tags_remove"}
    if not isinstance(ids, list) or not ids:
        raise ValueError("Select at least one game.")
    if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
        raise ValueError("No valid bulk changes were supplied.")
    if "tags" in changes and ("tags_add" in changes or "tags_remove" in changes):
        raise ValueError("Tags cannot be replaced and adjusted in the same request.")
    if "tags_add" in changes and "tags_remove" in changes:
        additions = {label.casefold() for label in normalize_tags(changes["tags_add"])}
        removals = {label.casefold() for label in normalize_tags(changes["tags_remove"])}
        if additions & removals:
            raise ValueError("A tag cannot be added and removed in the same request.")
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
        elif field == "tags":
            clean[field] = normalize_tags(value)
        elif field in ("tags_add", "tags_remove"):
            clean[field] = normalize_tags(value)
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
        if "tags" in patch or "tags_add" in patch or "tags_remove" in patch:
            apply_tag_changes(
                games[index],
                replace=patch.pop("tags", None),
                add=patch.pop("tags_add", None),
                remove=patch.pop("tags_remove", None),
            )
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
