"""Platform rename/delete with media migration.

Media migration is deliberately limited to files already stored below
``<media-root>/<old-name>/``; unrelated files are never touched. Every move is
rolled back if any later move fails.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path

from openbox import DATA
from pkg.state.media_probe import MEDIA_PATH_FIELDS, approved_media_path


_PLATFORM_SETTING_MAPS = (
    "platform_categories",
    "theme_by_platform",
    "image_group_by_platform",
    "cover_model_by_platform",
    "model_back_by_platform",
    "model_case_by_platform",
    "model_rotation_lock_by_platform",
    "platform_documents",
    "platform_videos",
    "emulator_platform_settings",
)


def _validate_platform_name(value):
    name = str(value or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Platform name is required and must be 80 characters or fewer.")
    if name in {".", ".."} or any(ch in name for ch in ("/", "\\", "\x00", "\r", "\n")):
        raise ValueError("Platform name may not contain a path separator.")
    return name


def _media_root() -> Path:
    root = DATA.parent / "media"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _rename_emulator_platforms(state: dict, old: str, new: str) -> None:
    entries = state.get("emulator_settings")
    if not isinstance(entries, dict):
        return
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        platforms = entry.get("platforms")
        if isinstance(platforms, dict) and old in platforms:
            platforms[new] = platforms.pop(old)


def _delete_emulator_platforms(state: dict, name: str) -> None:
    entries = state.get("emulator_settings")
    if not isinstance(entries, dict):
        return
    for entry in entries.values():
        if isinstance(entry, dict) and isinstance(entry.get("platforms"), dict):
            entry["platforms"].pop(name, None)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _move_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.is_file() and source.is_file() and _file_hash(source) == _file_hash(dest):
            source.unlink()
            return
        raise ValueError(f"Destination already exists: {dest}")
    try:
        source.replace(dest)
    except OSError:
        shutil.copy2(source, dest)
        # The cross-device fallback must reach disk before the source is
        # removed, otherwise a crash can lose both names.
        try:
            # Windows can only flush a handle opened for writing.
            descriptor = os.open(dest, os.O_RDWR)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
        source.unlink()


def _migrate_games_paths(games, old: str, new: str, media_root: Path) -> tuple[int, int, list]:
    moved: list[tuple[Path, Path]] = []
    moved_count = skipped = 0
    base = media_root.resolve()
    source_root = (media_root / old).resolve(strict=False)
    dest_root = (media_root / new).resolve(strict=False)
    if dest_root != base and base not in dest_root.parents:
        raise ValueError("Destination platform media path escapes the media root.")
    snapshots = [
        (game, {field: game.get(field) for field in MEDIA_PATH_FIELDS},
         list(game.get("screenshots") or []))
        for game in games
    ]
    try:
        for game in games:
            path_fields = [field for field in MEDIA_PATH_FIELDS if game.get(field)]
            values = [(field, str(game[field])) for field in path_fields]
            values += [("screenshots", str(path)) for path in game.get("screenshots") or []]
            for field, raw in values:
                try:
                    source = approved_media_path(raw, must_exist=True)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                if source_root not in source.parents:
                    skipped += 1
                    continue
                relative = source.relative_to(source_root)
                dest = dest_root / relative
                _move_file(source, dest)
                moved.append((source, dest))
                moved_count += 1
                if field == "screenshots":
                    game["screenshots"] = [str(dest) if str(item) == raw else item for item in game.get("screenshots") or []]
                else:
                    game[field] = str(dest)
    except (OSError, ValueError):
        for source, dest in reversed(moved):
            try:
                if dest.exists() and not source.exists():
                    dest.replace(source)
            except OSError:
                pass
        # The files went back; the metadata must go back with them or the
        # library points at destinations this call just rolled back.
        for game, fields, screenshots in snapshots:
            for field, original in fields.items():
                if original is None:
                    game.pop(field, None)
                else:
                    game[field] = original
            game["screenshots"] = screenshots
        raise
    return moved_count, skipped, moved


def _migrate_paths(state: dict, old: str, new: str, media_root: Path) -> tuple[int, int, list]:
    games = [game for game in state.get("games", []) if isinstance(game, dict) and str(game.get("platform") or "") == old]
    return _migrate_games_paths(games, old, new, media_root)


def migrate_game_media(games, old: str, new: str, *, media_root: Path | None = None) -> dict:
    """Move only the supplied games' media from ``<old>`` to ``<new>``.

    Raises ``ValueError``/``OSError`` after rolling back this call's own moves;
    the caller may keep the metadata change and report the error.
    """
    old = _validate_platform_name(old)
    new = _validate_platform_name(new)
    if old == new:
        return {"media_moved": 0, "media_skipped": 0}
    moved, skipped, _ = _migrate_games_paths(
        [game for game in games if isinstance(game, dict)], old, new, media_root or _media_root()
    )
    return {"media_moved": moved, "media_skipped": skipped}


def rename_platform(state: dict, old: str, new: str, *, migrate_media: bool = False, media_root: Path | None = None) -> dict:
    old = _validate_platform_name(old)
    new = _validate_platform_name(new)
    games = [game for game in state.get("games", []) if isinstance(game, dict)]
    existing = {str(game.get("platform") or "") for game in games}
    if old not in existing:
        raise ValueError(f"Platform not found: {old}")
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    occupied = set(existing)
    profiles = state.get("profiles")
    if isinstance(profiles, dict):
        occupied.update(str(key) for key in profiles)
    for key in _PLATFORM_SETTING_MAPS:
        mapping = settings.get(key)
        if isinstance(mapping, dict):
            occupied.update(str(name) for name in mapping)
    emulator_settings = state.get("emulator_settings")
    if isinstance(emulator_settings, dict):
        for entry in emulator_settings.values():
            if isinstance(entry, dict) and isinstance(entry.get("platforms"), dict):
                occupied.update(str(name) for name in entry["platforms"])
    if new in occupied and new != old:
        raise ValueError(f"Platform already exists: {new}")
    if old == new:
        return {"games": 0, "media_moved": 0, "media_skipped": 0}

    moved = skipped = 0
    if migrate_media:
        moved, skipped, _ = _migrate_paths(state, old, new, media_root or _media_root())

    changed = 0
    for game in games:
        if str(game.get("platform") or "") != old:
            continue
        game["platform"] = new
        changed += 1
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    for key in _PLATFORM_SETTING_MAPS:
        mapping = settings.get(key)
        if isinstance(mapping, dict) and old in mapping:
            mapping[new] = mapping.pop(old)
    profiles = state.get("profiles")
    if isinstance(profiles, dict) and old in profiles:
        profiles[new] = profiles.pop(old)
    _rename_emulator_platforms(state, old, new)
    for playlist in state.get("playlists", []):
        if not isinstance(playlist, dict) or playlist.get("type") != "filter":
            continue
        rules = playlist.get("rules")
        if isinstance(rules, dict) and rules.get("platform") == old:
            rules["platform"] = new
    for preset in settings.get("filter_presets", []) if isinstance(settings.get("filter_presets"), list) else []:
        rules = preset.get("rules") if isinstance(preset, dict) else None
        if isinstance(rules, dict) and rules.get("platform") == old:
            rules["platform"] = new
    return {"games": changed, "media_moved": moved, "media_skipped": skipped}


def delete_platform(state: dict, name: str, *, delete_media: bool = False, media_root: Path | None = None) -> dict:
    name = _validate_platform_name(name)
    games = state.get("games") if isinstance(state.get("games"), list) else []
    removed = [game for game in games if isinstance(game, dict) and str(game.get("platform") or "") == name]
    if not removed:
        raise ValueError(f"Platform not found: {name}")
    from handlers.library import _trash_entry_for  # local import avoids a cycle at module load

    trash = state.setdefault("trash", [])
    for index, game in enumerate(list(games)):
        if not isinstance(game, dict) or str(game.get("platform") or "") != name:
            continue
        entry = _trash_entry_for(state, game, index=index)
        entry["trashed_at"] = datetime.now().isoformat(timespec="seconds")
        trash.append(entry)
    state["games"] = [game for game in games if not (isinstance(game, dict) and str(game.get("platform") or "") == name)]
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    for key in _PLATFORM_SETTING_MAPS:
        mapping = settings.get(key)
        if isinstance(mapping, dict):
            mapping.pop(name, None)
    if isinstance(state.get("profiles"), dict):
        state["profiles"].pop(name, None)
    _delete_emulator_platforms(state, name)
    media_deleted = 0
    if delete_media:
        media_base = (media_root or _media_root()).resolve()
        root = media_base / name
        try:
            if root.is_symlink():
                raise ValueError("Platform media root is a symlink; refusing to delete outside the media root.")
            if root.resolve(strict=False).parent != media_base:
                raise ValueError("Platform media root escapes the media root.")
            if root.is_dir():
                for item in root.rglob("*"):
                    if item.is_file():
                        item.unlink()
                        media_deleted += 1
                for item in sorted(root.rglob("*"), reverse=True):
                    if item.is_dir():
                        item.rmdir()
                root.rmdir()
        except OSError:
            pass
    return {"games": len(removed), "media_deleted": media_deleted}
