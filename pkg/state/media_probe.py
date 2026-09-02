"""Media path validation, path probing, and media field definitions."""

import html
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.request import Request, urlopen

from backend_io import contained_path, download_file, read_limited
from openbox import DATA
from pkg.state.cache import (
    FILE_PROBE_CACHE,
    FILE_PROBE_LOCK,
    FILE_PROBE_MAX,
    FILE_PROBE_TTL,
    _SANITIZE_MEDIA_PATH_CACHE,
    _SANITIZE_MEDIA_PATH_LOCK,
    _SANITIZE_MEDIA_PATH_MAX,
    bump_media_epoch,
)

LOGGER = logging.getLogger("openbox")

FIELDS = {
    "name", "platform", "genre", "year", "developer", "publisher", "series",
    "collection", "description", "path", "launch", "launch_profile", "cover", "background",
    "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen",
    "cart_front", "cart_back", "disc", "advertisement", "manual",
    "source", "steam_app_id", "lutris_id", "install_dir",
    "heroic_app_id", "rom_name", "clone_of", "set_type", "ra_game_id", "ra_hash", "launchbox_db_id", "archive_member", "video", "music",
    "video_snap", "video_theme", "video_trailer", "video_recording",
    "progress", "rating", "notes", "region", "play_mode", "sort_title", "added_at",
    "alternate_names", "max_players", "wikipedia_url", "video_url", "hide_in_bigbox", "esrb",
    "broken", "portable", "controller_support", "disc_count",
    "gameyfin_id", "gameyfin_provider", "store_catalog", "store_installed", "owned",
    "tracking_mode", "tracking_delay", "tracking_frequency", "tracking_process_name", "igdb_id",
    "gamescope_preset",
}

# Media fields that can be populated from the LaunchBox Games Database. Kept as
# one set so metadata apply, bulk download, auto-import, and the audit all agree.
MEDIA_TYPES_ALL = {
    "cover", "background", "screenshots", "clear_logo", "fanart", "banner", "icon",
    "box_back", "box_spine", "box_3d", "title_screen",
    "cart_front", "cart_back", "disc", "advertisement", "manual",
}
MEDIA_ROOTS_ENV = "OPENBOX_MEDIA_ROOTS"
MEDIA_PATH_FIELDS = {
    "cover", "background", "clear_logo", "fanart", "banner", "icon",
    "box_back", "box_spine", "box_3d", "title_screen", "cart_front",
    "cart_back", "disc", "advertisement", "manual", "video", "music",
    "video_snap", "video_theme", "video_trailer", "video_recording",
}


def _ns(name, default):
    mod = sys.modules.get("webapp_state")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def probe_path(path, *, file_only=False):
    value = str(path or "")
    if not value:
        return False
    now = time.monotonic()
    key = (value, file_only)
    with FILE_PROBE_LOCK:
        cached = FILE_PROBE_CACHE.get(key)
        if cached is not None:
            if now - cached[0] < FILE_PROBE_TTL:
                FILE_PROBE_CACHE.move_to_end(key)
                return cached[1]
            FILE_PROBE_CACHE.pop(key, None)
    try:
        if file_only:
            st = os.stat(value)
            result = stat.S_ISREG(st.st_mode)
        else:
            os.stat(value)
            result = True
    except OSError:
        result = False
    with FILE_PROBE_LOCK:
        FILE_PROBE_CACHE[key] = (now, result)
        while len(FILE_PROBE_CACHE) > FILE_PROBE_MAX:
            FILE_PROBE_CACHE.popitem(last=False)
    return result


def _reject_media_symlink_components(path):
    cursor = Path(path)
    while True:
        try:
            if cursor.is_symlink():
                raise ValueError(f"Media paths may not contain symlinks: {cursor}")
        except OSError as error:
            raise ValueError(f"Could not inspect media path: {cursor}") from error
        if cursor.parent == cursor:
            return
        cursor = cursor.parent


def _media_roots():
    data_parent = _ns("DATA", DATA).parent
    values = [data_parent / "media"]
    configured = os.environ.get(MEDIA_ROOTS_ENV, "")
    if len(configured) > 16 * 4096:
        raise ValueError(f"{MEDIA_ROOTS_ENV} is too long.")
    for item in configured.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{MEDIA_ROOTS_ENV} entries must be absolute paths.")
        values.append(candidate)
    roots = []
    if len(values) > 32:
        raise ValueError(f"{MEDIA_ROOTS_ENV} contains too many roots.")
    for value in values:
        _reject_media_symlink_components(value)
        try:
            root = value.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Could not resolve media root: {value}") from error
        if root == Path("/"):
            raise ValueError("Filesystem root is not an approved media root.")
        if root not in roots:
            roots.append(root)
    return roots


def approved_media_path(path, *, must_exist=False):
    """Return a regular media path under DATA/media or explicit approved roots.

    Existing libraries stored outside OpenBox must opt in with
    ``OPENBOX_MEDIA_ROOTS`` (an absolute-path list separated by ``os.pathsep``).
    Paths outside those roots, and any symlinked component, are rejected before
    media or document handlers can use them.
    """
    raw = Path(str(path or "")).expanduser()
    if not str(path or "").strip() or not raw.is_absolute():
        raise ValueError("Media paths must be absolute paths under an approved media root.")
    _reject_media_symlink_components(raw)
    try:
        candidate = raw.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Could not resolve media path: {raw}") from error
    roots = _media_roots()
    if not any(candidate != root and root in candidate.parents for root in roots):
        raise ValueError(f"Path is outside an approved OpenBox media directory: {candidate}")
    if must_exist and not candidate.is_file():
        raise FileNotFoundError(str(candidate))
    return candidate


def safe_document_file(path):
    approved_fn = _ns("approved_media_path", approved_media_path)
    return approved_fn(path, must_exist=True)


def media_probe_path(path):
    approved_fn = _ns("approved_media_path", approved_media_path)
    probe_fn = _ns("probe_path", probe_path)
    try:
        return probe_fn(approved_fn(path), file_only=True)
    except (OSError, ValueError, TypeError):
        return False


def sanitize_media_path(path):
    approved_fn = _ns("approved_media_path", approved_media_path)
    max_san = _ns("_SANITIZE_MEDIA_PATH_MAX", _SANITIZE_MEDIA_PATH_MAX)
    value = str(path or "").strip()
    if not value:
        return ""
    with _SANITIZE_MEDIA_PATH_LOCK:
        cached = _SANITIZE_MEDIA_PATH_CACHE.get(value)
        if cached is not None:
            return cached
    try:
        result = str(approved_fn(value))
    except (OSError, ValueError, TypeError):
        result = ""
    with _SANITIZE_MEDIA_PATH_LOCK:
        if len(_SANITIZE_MEDIA_PATH_CACHE) >= max_san:
            _SANITIZE_MEDIA_PATH_CACHE.clear()
        _SANITIZE_MEDIA_PATH_CACHE[value] = result
    return result


def sanitize_document_records(records):
    approved_fn = _ns("approved_media_path", approved_media_path)
    if not isinstance(records, list):
        return []
    clean = []
    for item in records[:100]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        try:
            safe_path = approved_fn(path)
        except (OSError, ValueError, TypeError):
            continue
        record = dict(item)
        record["path"] = str(safe_path)
        clean.append(record)
    return clean


def approved_backup_file(path):
    data_parent = _ns("DATA", DATA).parent
    candidate = Path(str(path or "")).expanduser()
    if candidate.suffix.casefold() != ".zip":
        raise ValueError("Backups must be ZIP archives.")
    if candidate.is_symlink():
        raise ValueError("Backup path may not be a symlink.")
    return contained_path(candidate, [data_parent, data_parent / "backups"], must_exist=True)


def game_media_paths(game):
    """Return dictionary of present sanitized media paths for a game."""
    result = {}
    for field in MEDIA_PATH_FIELDS:
        val = str(game.get(field) or "").strip()
        if val:
            safe = sanitize_media_path(val)
            if safe:
                result[field] = safe
    return result


def download_image(url, destination):
    downloader = _ns("download_file", download_file)
    bump_epoch = _ns("bump_media_epoch", bump_media_epoch)
    result = str(downloader(
        url,
        destination,
        expected_types=("image/",),
        max_bytes=32 * 1024 * 1024,
        timeout=15,
        opener=urlopen,
    ))
    bump_epoch()
    return result


def update_steam_metadata(game):
    data_parent = _ns("DATA", DATA).parent
    app_id = str(game.get("steam_app_id", ""))
    if not app_id.isdigit():
        raise ValueError("This game has no Steam App ID.")
    request = Request(
        f"https://store.steampowered.com/api/appdetails?appids={app_id}",
        headers={"User-Agent": "OpenBox/1"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(read_limited(response, 4 * 1024 * 1024))
    record = payload.get(app_id, {})
    if not record.get("success"):
        raise ValueError("Steam did not return metadata for this game.")
    data = record["data"]
    game.update({
        "name": data.get("name") or game.get("name", ""),
        "developer": ", ".join(data.get("developers", [])),
        "publisher": ", ".join(data.get("publishers", [])),
        "genre": ", ".join(item["description"] for item in data.get("genres", [])),
        "year": data.get("release_date", {}).get("date", ""),
        "description": html.unescape(re.sub(r"<[^>]+>", "", data.get("short_description", ""))),
    })
    media = data_parent / "media" / "steam" / app_id
    try:
        game["cover"] = download_image(
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900_2x.jpg",
            media / "cover.jpg",
        )
    except (OSError, ValueError):
        pass
    if data.get("header_image"):
        try:
            game["background"] = download_image(data["header_image"], media / "background.jpg")
        except (OSError, ValueError):
            pass
