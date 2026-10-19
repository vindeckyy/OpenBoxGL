"""Media hygiene helpers: queues, region priority, cleanup, video categories."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

from backend_io import atomic_write_text
from pkg.platform_compat import lock_handle


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
        data = json.loads(path.read_text(encoding="utf-8"))
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
        with lock_handle(lock_file):
            yield


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
    size_buckets = {}
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
                size_buckets.setdefault(path.stat().st_size, []).append(path)
            except OSError:
                continue

    buckets = {}
    for paths in size_buckets.values():
        if len(paths) <= 1:
            continue
        for path in paths:
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
            # Keep a copy inside the allowed roots when present, so the retained file is the one the library can reference.
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


def cleanup_generated_cache(data_root, *, days=30, now=None):
    """Delete generated media cache files older than *days*.

    Only regular files under ``<data>/cache/media`` are removed. User media
    directories are never touched, and symlinks are skipped so the walk cannot
    escape the cache root.
    """
    import time
    from pathlib import Path

    root = Path(data_root).expanduser() / "cache" / "media"
    result = {"root": str(root), "deleted": 0, "bytes": 0, "skipped": 0}
    if not root.is_dir() or root.is_symlink():
        return result
    cutoff = (time.time() if now is None else float(now)) - max(0, int(days)) * 86400
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_mtime >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            result["skipped"] += 1
            continue
        result["deleted"] += 1
        result["bytes"] += info.st_size
    return result


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
            try:
                from webapp_state import sanitize_media_path
                if not sanitize_media_path(path):
                    continue
            except Exception:  # noqa: BLE001 - sanitize_media_path is external, may raise anything
                continue
            return field, path
    legacy = str(game.get("video") or "").strip()
    if legacy and Path(legacy).is_file():
        try:
            from webapp_state import sanitize_media_path
            if not sanitize_media_path(legacy):
                return "", ""
        except Exception:  # noqa: BLE001 - sanitize_media_path is external, may raise anything
            return "", ""
        return "video", legacy
    return "", ""


# ---------------------------------------------------------------------------
# Durable media download queue (P8.2)
# ---------------------------------------------------------------------------

MEDIA_QUEUE_MAX = 5000
MEDIA_QUEUE_MAX_ATTEMPTS = 3
MEDIA_QUEUE_BACKOFF = (5, 30, 120)
MEDIA_QUEUE_STATES = frozenset({"queued", "failed"})
MEDIA_QUEUE_BUILTIN_SOURCES = frozenset({
    "launchbox", "screenscraper", "igdb", "steamgriddb", "emumovies",
})
MEDIA_QUEUE_LEGACY_SOURCE = "launchbox"


def default_media_queue_path():
    from openbox import DATA

    return DATA.parent / "media-queue.json"


def _media_item_id(game_id, source, kind):
    digest = hashlib.sha1(f"{game_id}|{source}|{kind}".encode("utf-8")).hexdigest()[:12]
    return f"mq-{digest}"


def _clean_media_source(source, data=None):
    value = str(source or "").strip()
    if not value:
        data = data if isinstance(data, dict) else {}
        if str(data.get("source") or "").strip() in MEDIA_QUEUE_BUILTIN_SOURCES:
            value = str(data.get("source")).strip()
        elif data.get("launchbox_db_id"):
            value = "launchbox"
        elif data.get("emumovies"):
            value = "emumovies"
        elif data.get("steamgrid_id") or data.get("steamgriddb_id"):
            value = "steamgriddb"
        else:
            value = MEDIA_QUEUE_LEGACY_SOURCE
    if value in MEDIA_QUEUE_BUILTIN_SOURCES:
        return value
    if value.startswith("plugin:") and len(value) > len("plugin:"):
        return value
    raise ValueError(f"Unsupported media source: {value}")


def _valid_media_kind(kind):
    value = str(kind or "").strip()
    if not value:
        raise ValueError("A media kind is required.")
    try:
        from pkg.state.media_probe import MEDIA_TYPES_ALL
    except ImportError:  # pragma: no cover - runtime always has the package
        MEDIA_TYPES_ALL = set()
    if MEDIA_TYPES_ALL and value not in MEDIA_TYPES_ALL and value != "video_recording":
        raise ValueError(f"Unsupported media kind: {value}")
    return value


def _clean_attempts(value):
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        attempts = 0
    return max(0, min(MEDIA_QUEUE_MAX_ATTEMPTS, attempts))


def _clean_timestamp(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _item_kinds(item):
    kinds = []
    media = item.get("media")
    if isinstance(media, list):
        for value in media:
            kind = str(value or "").strip()
            if kind and kind not in kinds:
                kinds.append(kind)
    kind = str(item.get("kind") or "").strip()
    if kind and kind not in kinds:
        kinds.insert(0, kind)
    return kinds


def normalize_media_queue_item(item):
    """Normalize one queue entry, including jobs written by older releases."""
    if not isinstance(item, dict):
        return None
    data = dict(item)
    game_id = str(data.get("game_id") or "").strip()
    media = data.get("media")
    kinds = []
    if isinstance(media, list):
        kinds = [str(value).strip() for value in media if str(value or "").strip()]
    kind = str(data.get("kind") or "").strip()
    if not kind:
        kind = kinds[0] if kinds else "cover"
    source = _clean_media_source(data.get("source"), data)
    if kinds:
        data["media"] = kinds
    data["game_id"] = game_id
    data["source"] = source
    data["kind"] = kind
    data["url"] = str(data.get("url") or "")
    data["overwrite"] = bool(data.get("overwrite", False))
    data["attempts"] = _clean_attempts(data.get("attempts"))
    state = str(data.get("state") or "").strip()
    data["state"] = state if state in MEDIA_QUEUE_STATES else "queued"
    data["last_error"] = str(data.get("last_error") or "")[:500]
    data["next_attempt_at"] = _clean_timestamp(data.get("next_attempt_at"))
    data["id"] = str(data.get("id") or "").strip() or _media_item_id(game_id, source, kind)
    return data


def load_normalized_media_queue(queue_path):
    items = []
    for raw in load_media_queue(queue_path):
        try:
            item = normalize_media_queue_item(raw)
        except ValueError:
            continue
        if item is not None:
            items.append(item)
    return items


def _queue_key(item):
    return (str(item.get("game_id") or ""), str(item.get("source") or ""), str(item.get("kind") or ""))


def _append_media_item(items, item):
    key = _queue_key(item)
    for index, existing in enumerate(items):
        if _queue_key(existing) != key:
            continue
        if bool(existing.get("overwrite", False)) == bool(item.get("overwrite", False)):
            items[index] = item
            return False
        if len(items) >= MEDIA_QUEUE_MAX:
            raise ValueError(f"Media queue is full ({MEDIA_QUEUE_MAX} items).")
        items.append(item)
        return True
    if len(items) >= MEDIA_QUEUE_MAX:
        raise ValueError(f"Media queue is full ({MEDIA_QUEUE_MAX} items).")
    items.append(item)
    return True


def enqueue_media_job(queue_path, job_dict):
    """Enqueue one job (or a legacy job carrying a ``media`` list).

    Legacy import jobs list several media kinds in one dict; they are split
    into one durable item per kind so dedupe, limits, and retries operate at
    the unit that actually downloads. Duplicate ``(game_id, source, kind)``
    entries replace the existing entry unless the ``overwrite`` flag differs.
    """
    jobs = job_dict if isinstance(job_dict, list) else [job_dict]
    with _queue_lock(queue_path):
        items = load_normalized_media_queue(queue_path)
        for raw in jobs:
            if not isinstance(raw, dict):
                raise ValueError("Media queue jobs must be objects.")
            source = _clean_media_source(raw.get("source"), raw)
            media = raw.get("media")
            kinds = [str(value).strip() for value in media if str(value or "").strip()] if isinstance(media, list) else []
            kind = str(raw.get("kind") or "").strip()
            if not kinds and kind:
                kinds = [kind]
            if not kinds:
                kinds = ["cover"]
            for media_kind in kinds:
                item = normalize_media_queue_item({
                    **raw,
                    "source": source,
                    "kind": media_kind,
                    "media": [media_kind],
                })
                if item is None:
                    continue
                _valid_media_kind(media_kind)
                _append_media_item(items, item)
        save_media_queue(queue_path, items)
        return len(items)


def save_normalized_media_queue(queue_path, items):
    normalized = [item for item in (normalize_media_queue_item(raw) for raw in items) if item is not None]
    save_media_queue(queue_path, normalized)
    return normalized


def _persist_media_queue(queue_path, items):
    with _queue_lock(queue_path):
        save_media_queue(queue_path, items)


def retry_media_queue_item(queue_path, item_id):
    with _queue_lock(queue_path):
        items = load_normalized_media_queue(queue_path)
        changed = False
        for item in items:
            if str(item.get("id")) != str(item_id):
                continue
            item["state"] = "queued"
            item["attempts"] = 0
            item["last_error"] = ""
            item["next_attempt_at"] = 0.0
            changed = True
        if changed:
            save_media_queue(queue_path, items)
        return changed


def media_queue_snapshot(queue_path, *, running=False, paused=False, completed=0):
    items = load_normalized_media_queue(queue_path)
    completed = max(0, int(completed or 0))
    failed = sum(1 for item in items if item.get("state") == "failed")
    queued = sum(1 for item in items if item.get("state") != "failed")
    return {
        "items": items,
        "queue": items,
        "running": bool(running),
        "paused": bool(paused),
        "completed": completed,
        "failed": failed,
        "counts": {
            "total": len(items),
            "queued": queued,
            "failed": failed,
            "completed": completed,
        },
    }


def _field_for_kind(kind):
    if kind == "screenshots":
        return "screenshots"
    try:
        from pkg.state.media_probe import MEDIA_PATH_FIELDS
    except ImportError:  # pragma: no cover
        MEDIA_PATH_FIELDS = set()
    return kind if kind in MEDIA_PATH_FIELDS else ""


def _resolve_data_dir(data_dir=None):
    if data_dir:
        return Path(data_dir).expanduser()
    from openbox import DATA

    return Path(DATA).parent


def _metadata_database(state, data_dir):
    try:
        import webapp_state

        configured = getattr(webapp_state, "METADATA_DATABASE", None)
    except Exception:  # noqa: BLE001 - optional runtime binding
        configured = None
    if configured:
        return Path(configured)
    settings = state.get("settings") if isinstance(state, dict) else {}
    value = str((settings or {}).get("metadata_database") or "").strip()
    if value:
        return Path(value)
    candidate = _resolve_data_dir(data_dir) / "metadata" / "launchbox.db"
    return candidate if candidate.is_file() else None


def _download_steamgrid_item(game, kind, media_root):
    from pkg.parity import parity_steamgrid
    from pkg.parity.parity_metadata_providers import download_candidate_media

    game_id = game.get("steamgrid_id") or game.get("steamgriddb_id")
    if not game_id:
        raise ValueError("Game has no SteamGridDB match.")
    info = parity_steamgrid.game_info(int(game_id))
    entries = [entry for entry in info.get("media", []) if entry.get("kind") == kind]
    if not entries:
        raise ValueError(f"SteamGridDB has no {kind} artwork.")
    url = str(entries[0].get("url") or "")
    suffix = Path(url.split("?", 1)[0]).suffix or ".jpg"
    slug = "".join(character if character.isalnum() else "-" for character in str(game.get("name") or "game").casefold()).strip("-")
    return download_candidate_media(
        url,
        kind,
        media_root / "steamgriddb" / (slug or "game"),
        filename=f"{kind}{suffix}",
    )


def download_media_for_item(game, kind, item, state, data_dir=None):
    """Default provider dispatch for one queue kind. Returns a local path or ``""``."""
    source = str(item.get("source") or "")
    media_root = _resolve_data_dir(data_dir) / "media"
    if source == "launchbox":
        import metadata

        database_path = _metadata_database(state, data_dir)
        if database_path is None or not Path(database_path).is_file():
            raise ValueError("The LaunchBox metadata database is not installed.")
        database_id = game.get("launchbox_db_id")
        if not database_id:
            raise ValueError("Game has no LaunchBox database id.")
        download_kind = "cover" if kind in {"poster", "square"} else kind
        updated = metadata.apply_game_metadata(
            dict(game),
            database_path,
            int(database_id),
            [download_kind],
            media_root,
            overwrite=bool(item.get("overwrite", False)),
        )
        value = updated.get(download_kind)
        if download_kind == "screenshots":
            existing = {str(path) for path in game.get("screenshots") or []}
            for path in value or []:
                if str(path) not in existing:
                    return str(path)
            return ""
        return str(value or "") if value and value != game.get(kind) else ""
    if source == "emumovies":
        from pkg.parity.parity_emumovies import download_emumovies_media
        from pkg.parity.parity_integrations import load_emumovies_credentials

        credentials = load_emumovies_credentials(_resolve_data_dir(data_dir))
        return download_emumovies_media(game, credentials, media_root, kind)
    if source == "steamgriddb":
        return _download_steamgrid_item(game, kind, media_root)
    if source in {"screenscraper", "igdb"} or source.startswith("plugin:"):
        if source.startswith("plugin:"):
            from pkg.parity.parity_plugin_providers import download_plugin_media, plugin_media_search

            plugin_id = source.split(":", 1)[1]
            url = str(item.get("url") or "").strip()
            if url:
                return download_plugin_media(plugin_id, game, kind, {"url": url}, media_root)
            candidates = plugin_media_search(plugin_id, game, kind, limit=5)
            if not candidates:
                raise ValueError("The plugin returned no media for this game.")
            return download_plugin_media(
                plugin_id,
                game,
                kind,
                candidates[0],
                media_root,
            )
        from pkg.parity.parity_metadata_providers import (
            download_candidate_media,
            search_all,
        )
        from urllib.parse import urlsplit

        candidates = search_all(state, game, providers=[source], limit=5)
        for candidate in candidates:
            urls = (candidate.get("media") or {}).get(kind) or []
            if not isinstance(urls, list):
                continue
            for url in urls:
                if not isinstance(url, str) or not url.strip():
                    continue
                suffix = Path(urlsplit(url).path).suffix or ".jpg"
                slug = "".join(character if character.isalnum() else "-" for character in str(game.get("name") or "game").casefold()).strip("-") or "game"
                return download_candidate_media(
                    url,
                    kind,
                    media_root / source / slug,
                    filename=f"{kind}{suffix}",
                )
        raise ValueError(f"{source} has no {kind} media for this game.")
    raise ValueError(f"Unsupported media source: {source}")


def _emit_progress(progress, payload):
    if progress is None:
        return
    try:
        progress(payload)
    except Exception:  # noqa: BLE001 - progress reporting is advisory
        pass


def _find_game(state, game_id):
    for game in state.get("games", []) or []:
        if str(game.get("game_id")) == str(game_id):
            return game
    return None


def _apply_field(game, kind, path, overwrite):
    field = _field_for_kind(kind)
    if not field:
        return False
    if field == "screenshots":
        screenshots = game.get("screenshots")
        if not isinstance(screenshots, list):
            screenshots = []
        if overwrite:
            screenshots = [path]
        elif path not in screenshots:
            screenshots.append(path)
        else:
            return False
        game["screenshots"] = screenshots
        return True
    if game.get(field) and not overwrite:
        return False
    game[field] = path
    return True


def process_media_queue(
    queue_path=None,
    *,
    state=None,
    download=None,
    load_state=None,
    transact_state=None,
    now=time.time,
    cancel_event=None,
    pause_event=None,
    progress=None,
    data_dir=None,
    limit=None,
    per_group_limit=None,
):
    """Process queued media once. Safe to call without threads in tests."""
    queue_path = Path(queue_path or default_media_queue_path())
    if pause_event is not None and pause_event.is_set():
        return {"paused": True, "cancelled": False, "processed": 0, "completed": 0, "failed": 0, "skipped": 0}
    if cancel_event is not None and cancel_event.is_set():
        return {"paused": False, "cancelled": True, "processed": 0, "completed": 0, "failed": 0, "skipped": 0}

    provided_state = state is not None
    if not provided_state:
        loader = load_state
        if loader is None:
            from openbox import load_state as loader
        state = loader()
    settings = state.get("settings") if isinstance(state, dict) else {}
    settings = settings or {}
    if limit is None:
        try:
            limit = int(settings.get("media_download_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
    if per_group_limit is None:
        try:
            per_group_limit = int(settings.get("media_download_limit_per_group", 0) or 0)
        except (TypeError, ValueError):
            per_group_limit = 0
    limit = max(0, int(limit))
    per_group_limit = max(0, int(per_group_limit))

    items = load_normalized_media_queue(queue_path)
    summary = {
        "paused": False,
        "cancelled": False,
        "processed": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "limit_reached": False,
    }
    completed_games = set()
    group_counts = {}
    downloader = download or download_media_for_item

    def commit(mutate):
        if provided_state:
            mutate(state)
        elif transact_state is not None:
            transact_state(mutate)
        else:
            from openbox import update_state

            update_state(mutate)

    index = 0
    while index < len(items):
        if cancel_event is not None and cancel_event.is_set():
            summary["cancelled"] = True
            break
        if pause_event is not None and pause_event.is_set():
            summary["paused"] = True
            break
        item = items[index]
        if item.get("state") == "failed":
            index += 1
            continue
        if item.get("next_attempt_at") and float(item["next_attempt_at"]) > float(now()):
            index += 1
            continue
        game_id = str(item.get("game_id") or "")
        if limit and game_id not in completed_games and len(completed_games) >= limit:
            summary["limit_reached"] = True
            break
        game = _find_game(state, game_id)
        if game is None:
            item["last_error"] = "Game is no longer in the library."
            item["attempts"] = MEDIA_QUEUE_MAX_ATTEMPTS
            item["state"] = "failed"
            summary["failed"] += 1
            _persist_media_queue(queue_path, items)
            index += 1
            continue
        kinds = _item_kinds(item)
        failures = ""
        succeeded_any = False
        group_limited = False
        remaining = list(kinds)
        for kind in list(kinds):
            field = _field_for_kind(kind)
            if not field:
                summary["skipped"] += 1
                remaining.remove(kind)
                continue
            existing = game.get(field)
            if kind == "manual" and existing:
                summary["skipped"] += 1
                remaining.remove(kind)
                continue
            if existing and not item.get("overwrite", False):
                summary["skipped"] += 1
                remaining.remove(kind)
                continue
            group = (str(game.get("platform") or ""), kind)
            if per_group_limit and group_counts.get(group, 0) >= per_group_limit:
                summary["skipped"] += 1
                group_limited = True
                continue
            try:
                path = downloader(game, kind, item, state, data_dir)
            except Exception as error:  # noqa: BLE001 - one provider failure is recorded, not fatal
                failures = str(error)[:500]
                break
            if not path:
                summary["skipped"] += 1
                remaining.remove(kind)
                continue

            overwrite = bool(item.get("overwrite", False))

            def mutate(current, game_id=game_id, kind=kind, path=path, overwrite=overwrite):
                target = _find_game(current, game_id)
                if target is None:
                    return
                _apply_field(target, kind, str(path), overwrite)

            commit(mutate)
            group_counts[group] = group_counts.get(group, 0) + 1
            completed_games.add(game_id)
            succeeded_any = True
            remaining.remove(kind)
            summary["completed"] += 1
            _emit_progress(progress, {
                "event": "media-queue",
                "status": "completed",
                "item": item.get("id"),
                "game_id": game_id,
                "kind": kind,
                "path": str(path),
                "completed": summary["completed"],
                "failed": summary["failed"],
            })
        if group_limited and not failures and remaining:
            item["media"] = remaining
            item["kind"] = remaining[0]
            summary["limit_reached"] = True
            _persist_media_queue(queue_path, items)
            index += 1
            continue
        if failures:
            item["attempts"] = _clean_attempts(item.get("attempts")) + 1
            item["last_error"] = failures
            if remaining:
                item["media"] = remaining
                item["kind"] = remaining[0]
            if item["attempts"] >= MEDIA_QUEUE_MAX_ATTEMPTS:
                item["state"] = "failed"
                summary["failed"] += 1
            else:
                item["state"] = "queued"
                item["next_attempt_at"] = float(now()) + MEDIA_QUEUE_BACKOFF[item["attempts"] - 1]
            _emit_progress(progress, {
                "event": "media-queue",
                "status": "failed",
                "item": item.get("id"),
                "game_id": game_id,
                "error": failures,
                "attempts": item["attempts"],
                "completed": summary["completed"],
                "failed": summary["failed"],
            })
            if not remaining:
                items.pop(index)
                _persist_media_queue(queue_path, items)
                continue
            _persist_media_queue(queue_path, items)
            index += 1
            continue
        items.pop(index)
        _persist_media_queue(queue_path, items)
        summary["processed"] += 1
        if succeeded_any:
            _emit_progress(progress, {
                "event": "media-queue",
                "status": "done",
                "item": item.get("id"),
                "game_id": game_id,
                "completed": summary["completed"],
                "failed": summary["failed"],
                "remaining": len(items),
            })
        # The item was removed, so ``index`` still points at the next item.
    summary["processed"] = summary["completed"] + summary["skipped"] + summary["failed"]
    return summary


def media_queue_worker(
    cancel_event=None,
    queue_path=None,
    *,
    pause_event=None,
    state=None,
    download=None,
    load_state=None,
    transact_state=None,
    progress=None,
    data_dir=None,
    sleep=time.sleep,
    idle_seconds=5.0,
):
    """Daemon worker loop. Tests call :func:`process_media_queue` directly."""
    queue_path = Path(queue_path or default_media_queue_path())
    while cancel_event is None or not cancel_event.is_set():
        summary = process_media_queue(
            queue_path,
            state=state,
            download=download,
            load_state=load_state,
            transact_state=transact_state,
            cancel_event=cancel_event,
            pause_event=pause_event,
            progress=progress,
            data_dir=data_dir,
        )
        if summary.get("cancelled"):
            return summary
        if summary.get("paused"):
            sleep(idle_seconds)
            continue
        if summary.get("limit_reached") or not summary.get("processed"):
            sleep(idle_seconds)
    return {"cancelled": True}
