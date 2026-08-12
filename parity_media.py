"""Media hygiene helpers: queues, region priority, cleanup, video categories."""

from __future__ import annotations

import hashlib
import json
import fcntl
from contextlib import contextmanager
from pathlib import Path

from backend_io import atomic_write_text


REGION_PRIORITY_DEFAULT = ["North America", "World", "Europe", "Japan", ""]
VIDEO_FIELDS = ("video_snap", "video_theme", "video_trailer", "video_recording")
DEFAULT_VIDEO_PRIORITY = list(VIDEO_FIELDS)


def sort_images_by_region(images, region_priority=None):
    priority = list(region_priority or REGION_PRIORITY_DEFAULT)

    def rank(image):
        region = str(image.get("region") or "")
        try:
            index = priority.index(region)
        except ValueError:
            index = len(priority)
        return (index, str(image.get("filename") or ""))

    return sorted(images, key=rank)


def media_types_from_settings(settings):
    configured = settings.get("auto_import_media_types")
    if isinstance(configured, list) and configured:
        return {str(item) for item in configured}
    return {"cover", "background", "screenshots"}


def load_media_queue(queue_path):
    path = Path(queue_path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_media_queue(queue_path, items):
    path = Path(queue_path)
    atomic_write_text(path, json.dumps(items, indent=2), mode=0o600)


@contextmanager
def _queue_lock(queue_path):
    path = Path(queue_path)
    lock_path = path.with_name(f".{path.name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def enqueue_media_job(queue_path, job_dict):
    with _queue_lock(queue_path):
        items = load_media_queue(queue_path)
        items.append(dict(job_dict))
        save_media_queue(queue_path, items)
        return len(items)


def _fingerprint(path: Path):
    size = path.stat().st_size
    digest_builder = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    return f"{size}:{digest}"


def find_duplicate_media(games, allowed_roots=None):
    from catalog import MEDIA_FIELDS
    buckets = {}
    for game in games:
        paths = []
        for field in MEDIA_FIELDS:
            value = str(game.get(field) or "").strip()
            if value:
                paths.append(value)
        paths.extend(str(path).strip() for path in game.get("screenshots", []) if str(path).strip())
        for raw in paths:
            path = Path(raw).expanduser()
            if path.is_symlink() or not path.is_file():
                continue
            try:
                key = _fingerprint(path)
            except OSError:
                continue
            buckets.setdefault(key, []).append(str(path))
    roots = [Path(root).expanduser().resolve(strict=False) for root in (allowed_roots or [])]
    groups = []
    for paths in buckets.values():
        unique = list(dict.fromkeys(paths))
        if len(unique) > 1:
            # Prefer to keep a copy inside the allowed roots when present,
            # so the retained file is the one the library can reference.
            keep = unique[0]
            if roots:
                in_root = next(
                    (candidate for candidate in unique
                     if any(Path(candidate).expanduser().resolve() == root or root in Path(candidate).expanduser().resolve().parents for root in roots)),
                    None,
                )
                if in_root:
                    keep = in_root
            duplicates = [path for path in unique if path != keep]
            groups.append({"keep": keep, "duplicates": duplicates})
    return groups


def cleanup_duplicates(duplicate_groups, dry_run=True, allowed_roots=None):
    deleted = []
    roots = [Path(root).expanduser().resolve(strict=False) for root in (allowed_roots or [])]
    for group in duplicate_groups:
        for path in group.get("duplicates", []):
            target = Path(path)
            if dry_run:
                deleted.append(str(target))
                continue
            resolved = target.expanduser().resolve(strict=False)
            if target.is_symlink() or not target.is_file():
                continue
            if roots and not any(resolved == root or root in resolved.parents for root in roots):
                continue
            try:
                resolved.unlink(missing_ok=True)
                deleted.append(str(target))
            except OSError:
                pass
    return deleted


def normalize_video_fields(game):
    game.setdefault("video_snap", "")
    game.setdefault("video_theme", "")
    game.setdefault("video_trailer", "")
    game.setdefault("video_recording", "")
    legacy = str(game.get("video") or "").strip()
    if legacy and not game.get("video_snap"):
        game["video_snap"] = legacy
    return game


def active_video(game, priorities=None):
    normalize_video_fields(game)
    for field in priorities or DEFAULT_VIDEO_PRIORITY:
        path = str(game.get(field) or "").strip()
        if path and Path(path).is_file():
            return field, path
    legacy = str(game.get("video") or "").strip()
    if legacy and Path(legacy).is_file():
        return "video", legacy
    return "", ""
