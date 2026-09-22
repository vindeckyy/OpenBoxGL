"""Missing-file repair wizard: relink games and media after a move.

Health already reports missing paths; this module turns the report into a fix
without ever touching the filesystem state directly:

* ``scan_missing_paths`` projects the same missing-path information health
  shows, bounded and detached from the live state.
* ``scan_candidates`` walks a user-picked folder with hard entry/depth caps.
* ``plan_repair`` matches missing basenames against those candidates and keeps
  ambiguous matches out of the automatic plan.
* ``apply_repair`` re-validates every target against the state inside the
  caller's transaction and skips stale rows instead of clobbering them.

The client never sends replacement paths: preview and apply both derive the
plan from the chosen folder, so an apply cannot write an arbitrary path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from collections.abc import Iterable

MAX_SCAN_ITEMS = 2000
MAX_CANDIDATES = 20000
MAX_FOLDER_DEPTH = 8
MAX_PATH_LENGTH = 4096
MAX_AMBIGUOUS_CHOICES = 5

KIND_GAME = "game"
KIND_MEDIA = "media"

# Path-like game fields that may point at local media. Video/extra fields are
# validated by the same "missing path" rule as covers; launch metadata is not
# repaired here.
MEDIA_FIELDS = (
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

REPAIR_FIELDS = ("path",) + MEDIA_FIELDS


def resolve_folder(raw: Any) -> Path:
    """Validate a user-picked repair folder: absolute, bounded, not the root."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("A folder is required.")
    if len(text) > MAX_PATH_LENGTH:
        raise ValueError("Folder path is too long.")
    folder = Path(text).expanduser()
    if not folder.is_absolute():
        raise ValueError("Folder must be an absolute path.")
    try:
        resolved = folder.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Could not resolve folder: {folder}") from error
    if resolved == Path(resolved.anchor):
        raise ValueError("The filesystem root is not a repair folder.")
    if not resolved.is_dir():
        raise ValueError("Folder does not exist.")
    if resolved.is_symlink():
        raise ValueError("Folder must not be a symlink.")
    return resolved


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def scan_missing_paths(
    state: dict[str, Any],
    *,
    include_media: bool = True,
    limit: int = MAX_SCAN_ITEMS,
    is_file=None,
) -> dict[str, Any]:
    """Return bounded missing-path rows for games and (optionally) media.

    ``is_file`` is injectable so tests and callers can avoid repeated stat
    calls on the same path across request cycles.
    """
    checker = is_file or _is_file
    items: list[dict[str, Any]] = []
    truncated = False
    games = state.get("games") if isinstance(state, dict) else None
    if not isinstance(games, list):
        games = []
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "")
        name = str(game.get("name") or "")
        platform = str(game.get("platform") or "")
        fields = REPAIR_FIELDS if include_media else ("path",)
        for field in fields:
            raw = str(game.get(field) or "").strip()
            if not raw:
                continue
            if checker(Path(raw).expanduser()):
                continue
            if len(items) >= limit:
                truncated = True
                break
            items.append({
                "id": index,
                "game_id": game_id,
                "name": name,
                "platform": platform,
                "field": field,
                "path": raw,
                "kind": KIND_GAME if field == "path" else KIND_MEDIA,
            })
        if truncated:
            break
    return {"items": items, "count": len(items), "truncated": truncated}


def scan_candidates(
    folder: Any,
    *,
    limit: int = MAX_CANDIDATES,
    max_depth: int = MAX_FOLDER_DEPTH,
) -> list[dict[str, Any]]:
    """Walk a folder breadth-first with hard caps; only regular files."""
    root = resolve_folder(folder)
    candidates: list[dict[str, Any]] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue and len(candidates) < limit:
        directory, depth = queue.pop(0)
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if len(candidates) >= limit:
                break
            try:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 <= max_depth:
                        queue.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            candidates.append({
                "path": entry.path,
                "name": entry.name,
                "size": size,
                "relative": os.path.relpath(entry.path, root),
            })
    return candidates


def _match_keys(item: dict[str, Any]) -> list[str]:
    """Return the basenames the missing item may match, most specific first."""
    missing = Path(str(item.get("path") or "")).name.casefold()
    keys = [missing] if missing else []
    if item.get("kind") == KIND_GAME and missing:
        stem = Path(missing).stem
        if stem and stem != missing:
            keys.append(stem)
    return keys


def plan_repair(
    items: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
    *,
    fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Match missing basenames to folder candidates without guessing ties."""
    allowed = set(fields) if fields else None
    by_name: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("name"):
            continue
        name = str(candidate["name"]).casefold()
        by_name.setdefault(name, []).append(candidate)
        stem = Path(name).stem
        if stem and stem != name:
            by_name.setdefault(stem, []).append(candidate)

    matches: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if allowed is not None and item.get("field") not in allowed:
            continue
        row = {
            "id": item.get("id"),
            "game_id": item.get("game_id"),
            "name": item.get("name"),
            "platform": item.get("platform"),
            "field": item.get("field"),
            "kind": item.get("kind"),
            "from": item.get("path"),
        }
        choices: list[dict[str, Any]] = []
        seen_paths = set()
        for key in _match_keys(item):
            for candidate in by_name.get(key, []):
                if candidate["path"] in seen_paths:
                    continue
                seen_paths.add(candidate["path"])
                choices.append(candidate)
        choices.sort(key=lambda candidate: (len(str(candidate.get("relative") or "")), str(candidate.get("relative") or "")))
        if not choices:
            unmatched.append(row)
        elif len(choices) == 1:
            match = dict(row)
            match.update({
                "path": choices[0]["path"],
                "size": choices[0].get("size", 0),
                "relative": choices[0].get("relative", ""),
            })
            matches.append(match)
        else:
            ambiguous_row = dict(row)
            ambiguous_row["choices"] = [
                {"path": choice["path"], "relative": choice.get("relative", ""), "size": choice.get("size", 0)}
                for choice in choices[:MAX_AMBIGUOUS_CHOICES]
            ]
            ambiguous.append(ambiguous_row)
    return {
        "matches": matches,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "counts": {
            "matched": len(matches),
            "ambiguous": len(ambiguous),
            "unmatched": len(unmatched),
        },
    }


def apply_repair(
    state: dict[str, Any],
    matches: Iterable[dict[str, Any]],
    *,
    selection: Iterable[Any] | None = None,
    is_file=None,
) -> dict[str, Any]:
    """Apply a reviewed plan to state; stale or missing targets are skipped.

    Every row is re-validated here because the transaction may run after the
    preview (files can move, records can be edited). Selection keys are
    ``(id, field)`` pairs or plain game ids.
    """
    checker = is_file or _is_file
    selected_ids: set[Any] | None = None
    selected_pairs: set[tuple[Any, str]] | None = None
    if selection is not None:
        selected_ids = set()
        selected_pairs = set()
        for value in selection:
            if isinstance(value, (list, tuple)) and len(value) == 2:
                try:
                    selected_pairs.add((int(value[0]), str(value[1])))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    selected_ids.add(int(value))
                except (TypeError, ValueError):
                    continue
    games = state.get("games") if isinstance(state, dict) else None
    if not isinstance(games, list):
        raise ValueError("State has no games list.")
    updated = 0
    skipped: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    for row in matches:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("id"))
        except (TypeError, ValueError):
            skipped.append({"reason": "invalid_id", "row": row})
            continue
        field = str(row.get("field") or "")
        if field not in REPAIR_FIELDS:
            skipped.append({"reason": "invalid_field", "id": index, "field": field})
            continue
        if selected_pairs is not None and (index, field) not in selected_pairs and (selected_ids is None or index not in selected_ids):
            continue
        if index < 0 or index >= len(games) or not isinstance(games[index], dict):
            skipped.append({"reason": "missing_game", "id": index, "field": field})
            continue
        game = games[index]
        expected_id = str(row.get("game_id") or "")
        if expected_id and str(game.get("game_id") or "") != expected_id:
            skipped.append({"reason": "stale_game", "id": index, "field": field})
            continue
        target = Path(str(row.get("path") or "")).expanduser()
        if not checker(target):
            skipped.append({"reason": "missing_target", "id": index, "field": field, "path": str(target)})
            continue
        current = str(game.get(field) or "")
        from_value = str(row.get("from") or "")
        if current and current != from_value:
            skipped.append({"reason": "changed_since_preview", "id": index, "field": field})
            continue
        if current == str(target):
            continue
        game[field] = str(target)
        updated += 1
        applied.append({"id": index, "game_id": expected_id, "field": field, "path": str(target)})
    return {"updated": updated, "skipped": skipped, "applied": applied}


__all__ = [
    "KIND_GAME",
    "KIND_MEDIA",
    "MAX_AMBIGUOUS_CHOICES",
    "MAX_CANDIDATES",
    "MAX_FOLDER_DEPTH",
    "MAX_SCAN_ITEMS",
    "MEDIA_FIELDS",
    "REPAIR_FIELDS",
    "apply_repair",
    "plan_repair",
    "resolve_folder",
    "scan_candidates",
    "scan_missing_paths",
]
