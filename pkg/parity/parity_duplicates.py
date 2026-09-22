"""Duplicate detection and merge.

Three collision shapes are reported, in priority order:

* ``identity`` — canonical source identities colliding on two records, via
  :func:`parity_identity.detect_duplicate_identities` (no duplicated logic).
* ``path`` — two records pointing at the same normalized local path.
* ``title`` — the same normalized title on the same platform.

Detection is a read-only projection. Merging keeps the record with the most
play history (sessions, playtime, achievements) and unions media and list
fields from the duplicates into it. The duplicates are handed to a caller
supplied ``trash_game`` callback so the existing trash bin becomes the undo
mechanism: no merge silently drops a record.
"""

from __future__ import annotations

import os
import re
from typing import Any
from collections.abc import Callable, Iterable

from pkg.parity.parity_identity import detect_duplicate_identities
from state_store import prune_trash

MAX_GROUPS = 100
MAX_GROUP_GAMES = 25
MAX_LIST_ITEMS = 200
MAX_TITLE_LENGTH = 200

KIND_IDENTITY = "identity"
KIND_PATH = "path"
KIND_TITLE = "title"

MEDIA_UNION_FIELDS = (
    "cover",
    "background",
    "fanart",
    "banner",
    "clear_logo",
    "icon",
    "box_back",
    "box_spine",
    "box_3d",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
    "video",
    "music",
    "video_snap",
    "video_theme",
    "video_trailer",
)

LIST_UNION_FIELDS = (
    "tags",
    "save_paths",
    "applications",
    "versions",
    "documents",
    "alternate_names",
    "screenshots",
)

SCALAR_FILL_FIELDS = (
    "rating",
    "year",
    "developer",
    "publisher",
    "genre",
    "esrb",
    "progress",
    "release_date",
    "notes",
    "description",
    "cover_source",
)

# Edition / region / dump markers that must not stop a real duplicate from
# matching. Bracket contents are dropped wholesale; trailing tokens are only
# removed when they look like markers, never arbitrary words.
_BRACKET_RE = re.compile(r"[\(\[\{][^\)\]\}]{0,40}[\)\]\}]")
_TRAILING_MARKER_RE = re.compile(
    r"\b(usa|europe|japan|world|rev(ision)?\s*[a-z0-9]*|v\d+(\.\d+)*|disc\s*\d+|disk\s*\d+|en|jp|fr|de|es|pt|beta|proto|demo|unl|prg\d*)\b"
)


def normalize_title(value: Any) -> str:
    """Casefold a title and drop region/edition markers for matching."""
    text = str(value or "").casefold()
    text = _BRACKET_RE.sub(" ", text)
    text = _TRAILING_MARKER_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())[:MAX_TITLE_LENGTH]


def normalize_path(value: Any) -> str:
    """Normalize a path for equality without touching the filesystem."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.normpath(os.path.expanduser(raw))).casefold()


def _sessions_by_game(state: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    history = state.get("history") if isinstance(state, dict) else None
    if not isinstance(history, list):
        return counts
    for entry in history[:5000]:
        if not isinstance(entry, dict):
            continue
        game_id = str(entry.get("game_id") or "")
        if game_id:
            counts[game_id] = counts.get(game_id, 0) + 1
    return counts


def _media_count(game: dict[str, Any]) -> int:
    count = 0
    for field in MEDIA_UNION_FIELDS:
        if str(game.get(field) or "").strip():
            count += 1
    return count


def game_row(index: int, game: dict[str, Any], session_counts: dict[str, int]) -> dict[str, Any]:
    """Detached summary row used by the UI and the primary-selection score."""
    game_id = str(game.get("game_id") or "")
    return {
        "id": index,
        "game_id": game_id,
        "name": str(game.get("name") or ""),
        "platform": str(game.get("platform") or ""),
        "path": str(game.get("path") or ""),
        "playtime_seconds": int(game.get("playtime_seconds") or 0),
        "sessions": session_counts.get(game_id, 0),
        "achievements": int(game.get("ra_achievements_earned") or 0),
        "media": _media_count(game),
        "favorite": bool(game.get("favorite")),
        "progress": str(game.get("progress") or ""),
    }


def _group(kind: str, key: str, entries: list[tuple[int, dict[str, Any]]], session_counts) -> dict[str, Any]:
    first = entries[0][1]
    return {
        "kind": kind,
        "key": key,
        "title": str(first.get("name") or ""),
        "platform": str(first.get("platform") or ""),
        "games": [game_row(index, game, session_counts) for index, game in entries],
    }


def _identity_groups(games: list[dict[str, Any]]) -> list[tuple[str, list[tuple[int, dict[str, Any]]]]]:
    """Identity collision groups via canonical identities (parity_identity).

    Returns ``[(identity_key, [(index, game), ...]), ...]`` with at least two
    members per group. The game list is filtered to dicts before detection so
    indexes stay aligned with the caller's list.
    """
    dict_games = [game for game in games if isinstance(game, dict)]
    id_to_indexes: dict[str, list[int]] = {}
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            continue
        key = str(game.get("game_id") or game.get("id") or "")
        if key:
            id_to_indexes.setdefault(key, []).append(index)
    groups: list[tuple[str, list[tuple[int, dict[str, Any]]]]] = []
    for entry in detect_duplicate_identities(dict_games):
        members: list[tuple[int, dict[str, Any]]] = []
        for game_id in entry.get("games", []):
            indexes = id_to_indexes.get(str(game_id))
            # Consume one occurrence per reported game_id so two rows that
            # share the same game_id value map to two distinct indexes.
            index = indexes.pop(0) if indexes else None
            if index is not None:
                members.append((index, games[index]))
        if len(members) >= 2:
            groups.append((str(entry.get("identity") or ""), members))
    return groups


def find_duplicates(
    state: dict[str, Any],
    *,
    limit: int = MAX_GROUPS,
    include_title: bool = True,
) -> dict[str, Any]:
    """Bounded duplicate groups, most structural collisions first."""
    games = state.get("games") if isinstance(state, dict) else None
    if not isinstance(games, list):
        games = []
    session_counts = _sessions_by_game(state)
    buckets: list[tuple[str, dict[str, list[tuple[int, dict[str, Any]]]]]] = [
        (KIND_PATH, {}),
    ]
    if include_title:
        buckets.append((KIND_TITLE, {}))
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            continue
        path_key = normalize_path(game.get("path"))
        title_key = f"{normalize_title(game.get('name'))}\x00{str(game.get('platform') or '').casefold()}"
        for kind, bucket in buckets:
            if kind == KIND_PATH and path_key:
                bucket.setdefault(path_key, []).append((index, game))
            elif kind == KIND_TITLE and normalize_title(game.get("name")):
                bucket.setdefault(title_key, []).append((index, game))

    groups: list[dict[str, Any]] = []
    claimed: set[int] = set()
    truncated = False
    identity_groups = _identity_groups(games)

    def claim(kind: str, key: str, entries: list[tuple[int, dict[str, Any]]]) -> None:
        nonlocal truncated
        entries = [entry for entry in entries if entry[0] not in claimed]
        if len(entries) < 2:
            return
        if len(groups) >= limit:
            truncated = True
            return
        entries = entries[:MAX_GROUP_GAMES]
        groups.append(_group(kind, key, entries, session_counts))
        claimed.update(index for index, _game in entries)

    for identity_key, members in identity_groups:
        claim(KIND_IDENTITY, identity_key, members)
        if truncated:
            break
    if not truncated:
        for kind, bucket in buckets:
            for key in sorted(bucket):
                claim(kind, key, bucket[key])
                if truncated:
                    break
            if truncated:
                break
    return {
        "groups": groups,
        "count": len(groups),
        "truncated": truncated
        or any(len(entries) > MAX_GROUP_GAMES for _key, entries in identity_groups)
        or any(len(bucket) > MAX_GROUP_GAMES for _kind, bucket in buckets),
    }


def primary_choice(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the record to keep: history first, then media, then lowest index."""
    candidates = [row for row in rows if isinstance(row, dict)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            int(row.get("sessions") or 0),
            int(row.get("playtime_seconds") or 0),
            int(row.get("achievements") or 0),
            int(row.get("media") or 0),
            -int(row.get("id") or 0),
        ),
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, "", [], {}):
        return []
    return [value]


def _union_list(values: Iterable[Iterable[Any]]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for group in values:
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            if isinstance(item, dict):
                key = "|".join(sorted(str(item.get(key, "")) for key in ("path", "name", "id", "url")))
            else:
                key = str(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= MAX_LIST_ITEMS:
                return merged
    return merged


def merge_plan(state: dict[str, Any], indexes: Iterable[int]) -> dict[str, Any]:
    """Preview a merge without mutating state.

    ``indexes`` identifies the records in the group; the record with the most
    history is proposed as primary and every field change is reported so the
    UI can show a dry run before applying.
    """
    games = state.get("games") if isinstance(state, dict) else None
    if not isinstance(games, list):
        raise ValueError("State has no games list.")
    unique: list[int] = []
    for value in indexes:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(games) and index not in unique:
            unique.append(index)
    if len(unique) < 2:
        raise ValueError("A merge needs at least two records.")
    session_counts = _sessions_by_game(state)
    rows = [game_row(index, games[index], session_counts) for index in unique if isinstance(games[index], dict)]
    primary_row = primary_choice(rows)
    if primary_row is None:
        raise ValueError("A merge needs two valid records.")
    primary_index = int(primary_row["id"])
    primary = games[primary_index]
    duplicates = [index for index in unique if index != primary_index]
    changes: dict[str, Any] = {}
    for field in MEDIA_UNION_FIELDS:
        current = str(primary.get(field) or "").strip()
        if current:
            continue
        for index in duplicates:
            candidate = games[index]
            value = str(candidate.get(field) or "").strip()
            if value:
                changes[field] = {"value": value, "source": index}
                break
    for field in LIST_UNION_FIELDS:
        values = _union_list([_as_list(primary.get(field))])
        for index in duplicates:
            raw = games[index].get(field)
            if raw:
                values = _union_list([values, _as_list(raw)])
        if values and values != _as_list(primary.get(field)):
            changes[field] = {"value": values, "source": primary_index, "merged": True}
    for field in SCALAR_FILL_FIELDS:
        if str(primary.get(field) or "").strip():
            continue
        for index in duplicates:
            value = games[index].get(field)
            if value not in (None, "", [], {}):
                changes[field] = {"value": value, "source": index}
                break
    return {
        "primary": primary_row,
        "absorbed": [row for row in rows if row["id"] != primary_index],
        "changes": changes,
        "changed_fields": sorted(changes),
        "reason": {
            "sessions": primary_row["sessions"],
            "playtime_seconds": primary_row["playtime_seconds"],
            "achievements": primary_row["achievements"],
            "media": primary_row["media"],
        },
    }


def apply_merge(
    state: dict[str, Any],
    indexes: Iterable[int],
    *,
    trash_game: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply a merge; duplicates go to ``trash_game`` for undo when supplied.

    Without a ``trash_game`` callback the duplicates are removed outright, so
    HTTP callers should always pass the trash-entry factory (the merge route
    does) to keep the operation reversible.
    """
    plan = merge_plan(state, indexes)
    primary = state["games"][int(plan["primary"]["id"])]
    for field, change in plan["changes"].items():
        primary[field] = change["value"]
    duplicate_indexes = sorted((int(row["id"]) for row in plan["absorbed"]), reverse=True)
    trashed: list[dict[str, Any]] = []
    for index in duplicate_indexes:
        game = state["games"][index]
        entry = None
        if trash_game is not None:
            entry = trash_game(state, game)
            if isinstance(entry, dict):
                trashed.append(entry)
        member_keys = {str(game.get("game_id") or ""), str(game.get("id") or "")} - {""}
        for playlist in state.get("playlists", []):
            if not isinstance(playlist, dict) or playlist.get("type") != "manual":
                continue
            members = playlist.get("members")
            if isinstance(members, list):
                playlist["members"] = [member for member in members if str(member) not in member_keys]
        state["games"].pop(index)
    if trashed:
        trash = state.setdefault("trash", [])
        trash.extend(trashed)
        state["trash"] = prune_trash(trash)
    return {
        "merged": len(duplicate_indexes),
        "primary": plan["primary"],
        "trashed": [entry.get("name", "") for entry in trashed],
        "undo": "trash",
        "changed_fields": plan["changed_fields"],
    }


__all__ = [
    "KIND_IDENTITY",
    "KIND_PATH",
    "KIND_TITLE",
    "LIST_UNION_FIELDS",
    "MAX_GROUPS",
    "MAX_GROUP_GAMES",
    "MEDIA_UNION_FIELDS",
    "SCALAR_FILL_FIELDS",
    "apply_merge",
    "find_duplicates",
    "game_row",
    "merge_plan",
    "normalize_path",
    "normalize_title",
    "primary_choice",
]
