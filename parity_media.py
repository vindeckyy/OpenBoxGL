"""Media hygiene helpers: queues, region priority, cleanup, video categories."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, indent=2))
    temporary.replace(path)


def enqueue_media_job(queue_path, job_dict):
    items = load_media_queue(queue_path)
    items.append(dict(job_dict))
    save_media_queue(queue_path, items)
    return len(items)


def _fingerprint(path: Path):
    size = path.stat().st_size
    digest = hashlib.sha256(path.read_bytes()[:65536]).hexdigest()
    return f"{size}:{digest}"


def find_duplicate_media(games):
    buckets = {}
    for game in games:
        paths = []
        for field in ("cover", "background"):
            value = str(game.get(field) or "").strip()
            if value:
                paths.append(value)
        paths.extend(str(path).strip() for path in game.get("screenshots", []) if str(path).strip())
        for raw in paths:
            path = Path(raw).expanduser()
            if not path.is_file():
                continue
            try:
                key = _fingerprint(path)
            except OSError:
                continue
            buckets.setdefault(key, []).append(str(path))
    groups = []
    for paths in buckets.values():
        unique = list(dict.fromkeys(paths))
        if len(unique) > 1:
            groups.append({"keep": unique[0], "duplicates": unique[1:]})
    return groups


def cleanup_duplicates(duplicate_groups, dry_run=True):
    deleted = []
    for group in duplicate_groups:
        for path in group.get("duplicates", []):
            target = Path(path)
            if dry_run:
                deleted.append(str(target))
                continue
            try:
                target.unlink(missing_ok=True)
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
