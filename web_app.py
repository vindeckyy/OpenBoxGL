#!/usr/bin/env python3
"""Local browser UI for OpenBox. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC."""

import copy
import email.utils
import gzip
import json
import html
import logging
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse

from backend_io import contained_path, download_file, read_limited, remove_file_if_safe
from arcade import import_arcade
from automation import (
    DEFAULT_ATTEMPTS,
    DEFAULT_TIMEOUT,
    EVENT_TYPES,
    MAX_WEBHOOKS,
    build_event,
    test_ping,
    utc_now,
    validate_webhook,
)
from catalog import PROGRESS, apply_progress_automation, bulk_update, game_media_paths, related_game_ids, tag_counts
from api_errors import ApiError, BadRequest, GameNotFound, MediaNotFound, BadgeNotFound, DocumentNotFound, PlatformDocumentNotFound, RouteNotFound
from notifications import add_notification, clear as clear_notifications, mark_read as mark_notifications_read, unread_count
from routes import dispatch_get, dispatch_post
from play_queue import advance as advance_queue, enqueue as enqueue_queue, remove as remove_queue, reorder as reorder_queue, resolve_queue

from cloud_sync import sync_statistics
from crash_report import build_report
from emulators import emulator_status, install_all_emulators, install_emulator, launch_emulator, recommendations_for_platform, update_all_emulators, update_emulator
from importers import import_heroic, import_lutris, import_steam
from metadata import apply_game_metadata, search_games, sync_database
from job_manager import JobManager
from openbox_logging import configure_logging, read_diagnostic_log
from openbox import DATA, EXTENSIONS, PLATFORM_BY_EXTENSION, STATE_STORE, build_launch, discover_profiles, load_state, purge_demo_games, recover_state as recover_library_state, update_state, update_state_with_result
from state_store import StateCorruptError, secure_text_write
from settings_schema import KNOWN_SETTINGS, sanitize_settings
from env_config import bootstrap_env
from parity_discovery import discovery_lists, related_with_reasons
from parity_import import detect_dependencies, import_multi_platform, import_rpcs3_hdd, import_scummvm, import_vita3k, recommend_emulators
from parity_integrations import (
    attach_recording, auto_attach_obs_recording, capture_screenshot, download_bezel, download_emumovies_media,
    export_highscores, import_highscores, inject_retroachievements, load_emumovies_credentials, obs_recording_status,
    read_local_highscores, save_emumovies_credentials,
)
from parity_media import (
    REGION_PRIORITY_DEFAULT, active_video, cleanup_duplicates, enqueue_media_job,
    find_duplicate_media, load_media_queue, media_types_from_settings, normalize_video_fields,
)
from parity_saves import enforce_backup_limit, extra_save_candidates, games_with_saves, scan_all_saves
from parity_storefront import catalog_entries_to_games, storefront_catalog
from parity_gameyfin import (
    GameyfinError,
    catalog_gameyfin,
    install_gameyfin_game,
    test_gameyfin_connection,
    uninstall_gameyfin_game,
)
from parity_save_tools import run_hoard, run_ludusavi, save_tool_status
from parity_filter_presets import (
    bigbox_quick_presets,
    delete_preset,
    explorer_facets,
    list_presets,
    save_preset,
)
from parity_deeplinks import handle_cli, launcher_menu_items
from parity_gamescope import (
    OPENBOX_STEAM_GAME_ID,
    is_gamescope_guest,
    is_steam_launch,
    mark_process_windows,
    open_ui,
    steam_game_id_for,
)
from parity_backup import BACKUP_ITEMS, create_backup, restore_backup
from parity_perf import apply_perf_profile, effective_profile_name, restore_perf_profile
from parity_tracking import close_store_client, wait_for_exit, TRACKING_MODES
from parity_igdb import apply_to_game as apply_igdb_metadata, fetch_game as fetch_igdb_game, search_games as search_igdb_games
from parity_emulator_defs import (
    load_definitions,
    merge_profiles_from_definitions,
    save_scan_config,
    scan_folder as scan_emulator_folder,
    list_scan_configs,
)
from parity_import_policy import add_exclusion, filter_imported, list_exclusions, remove_exclusion
from stock_themes import ensure_stock_themes
from parity_premium import (
    LIST_COLUMNS_DEFAULT,
    apply_media_pack,
    bulk_wizard_changes,
    category_for_platform,
    custom_field_defs,
    download_gog_media,
    download_steam_trailer,
    enhanced_ra_profile,
    import_loose_arcade,
    import_with_emulator_choice,
    import_xbox360_folder,
    list_media_packs,
    normalize_custom_fields,
    platform_categories,
    strings_for,
)
from plugin_catalog import download_plugin_package, fetch_plugin_catalog
from plugins import install_plugin, list_plugins, remove_plugin, run_plugins, set_plugin_enabled
from retroachievements import api_get as ra_api_get, game_progress as ra_game_progress, load_credentials as load_ra_credentials, match_game as match_ra_game, save_credentials as save_ra_credentials
from saves import backup_saves, discover_save_paths, list_backups, restore_saves
from updates import VERSION, check_update, install_desktop_entry, install_update

ROOT = Path(__file__).parent
TOKEN = secrets.token_urlsafe(24)
# JSON payloads at or above this size get gzip when the client accepts it.
GZIP_THRESHOLD = 1024
LOGGER = logging.getLogger("openbox")
STATE_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
RUNNING = {}
PROCESSES = {}
SESSION_EVENTS = []
EVENT_SEQUENCE = 0
INSTALLS = {}
METADATA_JOB = {}
MEDIA_JOB = {}
JOB_MANAGER = JobManager()
FILE_PROBE_CACHE = {}
FILE_PROBE_LOCK = threading.Lock()
FILE_PROBE_TTL = 60.0
PLUGIN_LIBRARY_CACHE = {"at": 0.0, "payload": None}
PLUGIN_LIBRARY_TTL = 3.0
PLUGIN_LIBRARY_LOCK = threading.Lock()
MEDIA_EPOCH = {"value": 0}
MEDIA_EPOCH_LOCK = threading.Lock()
PLUGIN_EPOCH = {"value": 0}
PUBLIC_STATE_CACHE = {"signature": None, "payload": None, "raw": None}
PUBLIC_STATE_LOCK = threading.Lock()
STATE_VIEW_CACHE = {"signature": None, "state": None}
STATE_VIEW_LOCK = threading.Lock()
WATCH_STOP = threading.Event()
METADATA_DATABASE = DATA.parent / "metadata/launchbox.db"
SERVER_PORT = 0
WEBHOOK_DISPATCHER = None
WEBHOOK_DISPATCHER_LOCK = threading.Lock()
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
}


# Media fields that can be populated from the LaunchBox Games Database. Kept as
# one set so metadata apply, bulk download, auto-import, and the audit all agree.
MEDIA_TYPES_ALL = {
    "cover", "background", "screenshots", "clear_logo", "fanart", "banner", "icon",
    "box_back", "box_spine", "box_3d", "title_screen",
    "cart_front", "cart_back", "disc", "advertisement", "manual",
}


def probe_path(path, *, file_only=False):
    value = str(path or "")
    if not value:
        return False
    now = time.monotonic()
    key = (value, file_only)
    with FILE_PROBE_LOCK:
        cached = FILE_PROBE_CACHE.get(key)
        if cached and now - cached[0] < FILE_PROBE_TTL:
            return cached[1]
    candidate = Path(value)
    result = candidate.is_file() if file_only else candidate.exists()
    with FILE_PROBE_LOCK:
        FILE_PROBE_CACHE[key] = (now, result)
        if len(FILE_PROBE_CACHE) > 10000:
            cutoff = now - FILE_PROBE_TTL
            for cached_key, (created, _) in list(FILE_PROBE_CACHE.items()):
                if created < cutoff:
                    FILE_PROBE_CACHE.pop(cached_key, None)
    return result


def safe_document_file(path):
    candidate = Path(str(path or "")).expanduser()
    if candidate.is_symlink():
        raise ValueError("Symlinked documents are not supported.")
    if not candidate.is_file():
        raise FileNotFoundError(str(candidate))
    return candidate


def approved_backup_file(path):
    candidate = Path(str(path or "")).expanduser()
    if candidate.suffix.casefold() != ".zip":
        raise ValueError("Backups must be ZIP archives.")
    if candidate.is_symlink():
        raise ValueError("Backup path may not be a symlink.")
    return contained_path(candidate, [DATA.parent, DATA.parent / "backups"], must_exist=True)


def game_identity(game):
    if game.get("steam_app_id"):
        return "steam", str(game["steam_app_id"])
    if game.get("heroic_app_id"):
        return "heroic", str(game.get("source", "")), str(game["heroic_app_id"])
    if game.get("lutris_id"):
        return "lutris", str(game["lutris_id"])
    if game.get("rom_name"):
        return "arcade", str(game.get("source", "")), str(game["rom_name"])
    return "path", str(Path(game.get("path", "")).expanduser())


def import_folder_path(folder, recommend=True, chosen_emulators=None):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    if chosen_emulators:
        candidates, recommendations = import_with_emulator_choice(
            folder, EXTENSIONS, PLATFORM_BY_EXTENSION, chosen_emulators
        )
    else:
        candidates = import_multi_platform(folder, EXTENSIONS, PLATFORM_BY_EXTENSION)
        recommendations = {}
        for item in candidates:
            platform = item.get("platform", "")
            recommendations.setdefault(platform, recommend_emulators(platform))
    result = {"additions": [], "settings": {}, "recommendations": recommendations}

    def mutate(state):
        existing = {game.get("path") for game in state["games"]}
        settings = state.get("settings", {})
        additions = []
        selected_recommendations = recommendations if recommend else {}
        for item in candidates:
            if item["path"] in existing:
                continue
            normalize_video_fields(item)
            additions.append(item)
            if recommend:
                platform = item.get("platform", "")
                selected_recommendations.setdefault(platform, recommend_emulators(platform))
        if additions:
            state["games"].extend(additions)
        result.update({"additions": additions, "settings": dict(settings), "recommendations": selected_recommendations})

    committed = update_state(mutate)
    added_paths = {str(game.get("path")) for game in result["additions"]}
    result["additions"] = [
        game for game in committed["games"] if str(game.get("path")) in added_paths
    ]
    media_types = media_types_from_settings(result["settings"])
    limit = int(result["settings"].get("media_download_limit", 0) or 0)
    queue_path = DATA.parent / "media-queue.json"
    queued = 0
    for game in result["additions"]:
        if limit and queued >= limit:
            break
        if game.get("launchbox_db_id"):
            enqueue_media_job(queue_path, {
                "name": game.get("name"),
                "path": game.get("path"),
                "game_id": game.get("game_id"),
                "media": sorted(media_types),
            })
            queued += 1
    return len(result["additions"]), len(candidates), result["recommendations"]


def merge_imported_games(imported, identity_fn):
    result = {"added": 0, "found": 0}

    def mutate(state):
        filtered = filter_imported(imported, state)
        existing = {identity_fn(game) for game in state["games"]}
        new_games = [game for game in filtered if identity_fn(game) not in existing]
        timestamp = datetime.now().isoformat(timespec="seconds")
        default_progress = state.get("settings", {}).get("progress_on_first_play", "Playing")
        for game in new_games:
            game["added_at"] = timestamp
            if default_progress and not game.get("progress"):
                game["progress"] = default_progress
            normalize_video_fields(game)
        state["games"].extend(new_games)
        result.update({"added": len(new_games), "found": len(filtered)})

    update_state(mutate)
    return result["added"], result["found"]


def auto_import_worker(cancel_event=None):
    delay = 10
    while not WATCH_STOP.wait(delay):
        if cancel_event and cancel_event.is_set():
            return
        try:
            state = load_state()
        except Exception as error:
            LOGGER.exception("Automatic import paused because library state could not be read: %s", error)
            delay = min(delay * 2, 300)
            continue
        delay = 10
        settings = state.get("settings", {})
        folders = settings.get("watch_folders", [])
        for folder in folders:
            try:
                import_folder_path(folder)
            except (OSError, ValueError) as error:
                LOGGER.warning("Watched-folder import failed for %s: %s", folder, error)
        storefront = settings.get("storefront_auto_import", {})
        if storefront.get("steam"):
            try:
                merge_imported_games(import_steam(), lambda game: ("steam", str(game.get("steam_app_id", ""))))
            except (OSError, ValueError) as error:
                LOGGER.warning("Steam auto-import failed: %s", error)
        if storefront.get("heroic"):
            try:
                merge_imported_games(
                    import_heroic(),
                    lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", ""))),
                )
            except (OSError, ValueError) as error:
                LOGGER.warning("Heroic auto-import failed: %s", error)
        if storefront.get("lutris"):
            try:
                merge_imported_games(import_lutris(), lambda game: ("lutris", str(game.get("lutris_id", ""))))
            except (OSError, ValueError) as error:
                LOGGER.warning("Lutris auto-import failed: %s", error)
        if storefront.get("gameyfin"):
            try:
                catalog, _providers = catalog_gameyfin(settings)
                imported = catalog_entries_to_games(catalog)
                merge_imported_games(imported, lambda game: ("gameyfin", str(game.get("gameyfin_id", ""))))
            except (OSError, ValueError, GameyfinError) as error:
                LOGGER.warning("Gameyfin auto-import failed: %s", error)
        for config in list_scan_configs(state):
            if not config.get("auto_update"):
                continue
            folder = str(config.get("folder", "")).strip()
            if not folder:
                continue
            try:
                imported = scan_emulator_folder(folder)
                merge_imported_games(imported, lambda game: ("path", str(game.get("path", ""))))
            except (OSError, ValueError) as error:
                LOGGER.warning("Emulator scan auto-update failed for %s: %s", folder, error)


def clean_commands(commands):
    if not isinstance(commands, list) or len(commands) > 25:
        raise ValueError("Application commands must be a list of at most 25 entries.")
    clean = []
    for command in commands:
        command = str(command).strip()
        if command:
            if not shlex.split(command):
                raise ValueError("Application command is empty.")
            clean.append(command)
    return clean


def run_configured_commands(key):
    for command in load_state().get("settings", {}).get(key, []):
        try:
            args = shlex.split(command)
            args[0] = str(Path(args[0]).expanduser())
            subprocess.Popen(args, start_new_session=True)
        except (OSError, ValueError, IndexError):
            pass


def public_settings(state=None):
    state = state or load_state()
    settings = state.get("settings", {})
    return {
        "watch_folders": settings.get("watch_folders", []),
        "screensaver_seconds": settings.get("screensaver_seconds", 90),
        "controller_map": settings.get("controller_map", {}),
        "badge_visibility": settings.get("badge_visibility", ["favorite", "installed", "saves", "documents", "progress", "storefront", "achievements", "rating"]),
        "image_group": settings.get("image_group", "cover"),
        "image_group_by_platform": settings.get("image_group_by_platform", {}),
        "image_group_by_playlist": settings.get("image_group_by_playlist", {}),
        "cloud_folder": settings.get("cloud_folder", ""),
        "last_cloud_sync": settings.get("last_cloud_sync", ""),
        "startup_commands": settings.get("startup_commands", []),
        "shutdown_commands": settings.get("shutdown_commands", []),
        "track_session_history": settings.get("track_session_history", True),
        "backup_on_close": settings.get("backup_on_close", False),
        "save_backup_limit": settings.get("save_backup_limit", 10),
        "progress_automation_enabled": settings.get("progress_automation_enabled", False),
        "progress_automation_play_minutes": settings.get("progress_automation_play_minutes", 30),
        "progress_automation_idle_days": settings.get("progress_automation_idle_days", 30),
        "welcome_completed": settings.get("welcome_completed", False),
        "auto_import_media_types": sorted(media_types_from_settings(settings)),
        "media_download_limit": settings.get("media_download_limit", 0),
        "region_priority": settings.get("region_priority", list(REGION_PRIORITY_DEFAULT)),
        "video_priority": settings.get("video_priority", ["video_snap", "video_theme", "video_trailer", "video_recording"]),
        "library_music": settings.get("library_music", ""),
        "video_bgm_mix": settings.get("video_bgm_mix", False),
        "bigbox_mode": settings.get("bigbox_mode", "stage"),
        "show_playlist_actions": settings.get("show_playlist_actions", True),
        "sidebar_sections": settings.get("sidebar_sections", ["search", "view", "platforms", "playlists", "filters"]),
        "hidden_sidebar_sections": settings.get("hidden_sidebar_sections", []),
        "storefront_auto_import": settings.get("storefront_auto_import", {"steam": False, "heroic": False, "lutris": False, "gameyfin": False}),
        "obs_auto_attach": settings.get("obs_auto_attach", True),
        "obs_recording_path": settings.get("obs_recording_path", ""),
        "dynamic_play_button": settings.get("dynamic_play_button", True),
        "gameyfin_url": settings.get("gameyfin_url", ""),
        "gameyfin_username": settings.get("gameyfin_username", ""),
        "gameyfin_password_set": bool(settings.get("gameyfin_password")),
        "gameyfin_install_dir": settings.get("gameyfin_install_dir", ""),
        "gameyfin_provider": settings.get("gameyfin_provider", ""),
        "ludusavi_backup_path": settings.get("ludusavi_backup_path", ""),
        "save_tools": save_tool_status(),
        "platform_documents": settings.get("platform_documents", {}),
        "custom_field_defs": custom_field_defs(settings),
        "platform_categories": platform_categories(settings),
        "list_columns": settings.get("list_columns", list(LIST_COLUMNS_DEFAULT)),
        "library_view": settings.get("library_view", "grid"),
        "locale": settings.get("locale", "en"),
        "strings": strings_for(settings.get("locale", "en")),
        "attract_mode_seconds": settings.get("attract_mode_seconds", settings.get("screensaver_seconds", 90)),
        "bigbox_startup_video": settings.get("bigbox_startup_video", ""),
        "bigbox_shutdown_commands": settings.get("bigbox_shutdown_commands", []),
        "tray_enabled": settings.get("tray_enabled", False),
        "minimize_to_tray": settings.get("minimize_to_tray", False),
        "ui_window": settings.get("ui_window", "app"),
        "media_packs": list_media_packs(settings),
        "controller_prompt_hint": settings.get("controller_prompt_hint", ""),
        "premium_features_free": True,
        "progress_on_first_play": settings.get("progress_on_first_play", "Playing"),
        "tracking_mode": settings.get("tracking_mode", "default"),
        "tracking_delay": settings.get("tracking_delay", 0),
        "tracking_frequency": settings.get("tracking_frequency", 2),
        "apply_perf": settings.get("apply_perf", "auto"),
        "auto_close_store_clients": settings.get("auto_close_store_clients", False),
        "filter_presets": list_presets(state),
        "import_exclusions": list_exclusions(state),
        "emulator_scan_configs": list_scan_configs(state),
        "safe_mode": bool(os.environ.get("OPENBOX_SAFE_MODE")),
        "emumovies_configured": bool(load_emumovies_credentials(DATA.parent).get("username")),
        "version": VERSION,
        "appimage": bool(os.environ.get("APPIMAGE")),
        "gamescope_guest": is_gamescope_guest(force="--game-mode" in sys.argv),
    }


def _public_state_signature():
    return (STATE_STORE.signature(), MEDIA_EPOCH["value"], PLUGIN_EPOCH["value"])


def _build_public_state():
    with STATE_LOCK:
        state = load_state()
    save_indices = set(games_with_saves(state["games"]))
    games = []
    for index, game in enumerate(state["games"]):
        projected = dict(game)
        normalize_video_fields(projected)
        visible = {key: projected.get(key, "") for key in FIELDS}
        video_field, video_path = active_video(projected, state.get("settings", {}).get("video_priority"))
        path_exists = probe_path(projected.get("path"))
        store_installed = bool(projected["store_installed"]) if "store_installed" in projected else path_exists
        visible.update({
            "id": index,
            "game_id": projected.get("game_id", ""),
            "favorite": bool(projected.get("favorite")),
            "hidden": bool(projected.get("hidden")),
            "hide_in_bigbox": bool(projected.get("hide_in_bigbox")),
            "last_played": projected.get("last_played", ""),
            "play_count": projected.get("play_count", 0),
            "playtime_seconds": projected.get("playtime_seconds", 0),
            "path_exists": path_exists,
            "has_cover": probe_path(projected.get("cover"), file_only=True),
            "has_background": probe_path(projected.get("background"), file_only=True),
            "has_clear_logo": probe_path(projected.get("clear_logo"), file_only=True),
            "has_fanart": probe_path(projected.get("fanart"), file_only=True),
            "has_banner": probe_path(projected.get("banner"), file_only=True),
            "has_icon": probe_path(projected.get("icon"), file_only=True),
            "has_box_back": probe_path(projected.get("box_back"), file_only=True),
            "has_box_spine": probe_path(projected.get("box_spine"), file_only=True),
            "has_box_3d": probe_path(projected.get("box_3d"), file_only=True),
            "has_title_screen": probe_path(projected.get("title_screen"), file_only=True),
            "has_cart_front": probe_path(projected.get("cart_front"), file_only=True),
            "has_cart_back": probe_path(projected.get("cart_back"), file_only=True),
            "has_disc": probe_path(projected.get("disc"), file_only=True),
            "has_advertisement": probe_path(projected.get("advertisement"), file_only=True),
            "has_manual": probe_path(projected.get("manual"), file_only=True),
            "has_video": bool(video_path),
            "active_video_field": video_field,
            "has_music": probe_path(projected.get("music"), file_only=True),
            "has_saves": index in save_indices or bool(projected.get("save_paths")),
            "has_documents": bool(projected.get("documents")),
            "has_versions": bool(projected.get("versions")),
            "has_achievements": bool(projected.get("ra_game_id")),
            "has_highscores": bool(projected.get("rom_name")) and str(projected.get("platform", "")).casefold() in {"arcade", "mame", "finalburn neo"},
            "has_missing_media": not probe_path(projected.get("cover"), file_only=True),
            "extract_archive": bool(projected.get("extract_archive")),
            "applications": projected.get("applications", []),
            "versions": projected.get("versions", []),
            "documents": projected.get("documents", []),
            "save_paths": projected.get("save_paths", []),
            "screenshots": projected.get("screenshots", []),
            "alternate_names": projected.get("alternate_names", []) if isinstance(projected.get("alternate_names"), list) else [name for name in str(projected.get("alternate_names") or "").split(";") if name.strip()],
            "available_screenshots": [
                index for index, path in enumerate(projected.get("screenshots", []))
                if probe_path(path, file_only=True)
            ],
            "esrb": projected.get("esrb", ""),
            "custom_fields": projected.get("custom_fields", {}) if isinstance(projected.get("custom_fields"), dict) else {},
            "platform_category": category_for_platform(projected.get("platform", ""), state.get("settings", {})),
            "tags": list(projected.get("tags", [])) if isinstance(projected.get("tags"), list) else [],
            "store_catalog": bool(projected.get("store_catalog")),
            "store_installed": store_installed,
            "owned": bool(projected.get("owned") or projected.get("store_catalog") or projected.get("steam_app_id") or projected.get("heroic_app_id") or projected.get("lutris_id") or projected.get("gameyfin_id")),
            "installable": bool(projected.get("gameyfin_id")) and not store_installed,
            "gameyfin_id": projected.get("gameyfin_id", ""),
        })
        games.append(visible)
    decorated = games
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        now = time.monotonic()
        cached = PLUGIN_LIBRARY_CACHE
        with PLUGIN_LIBRARY_LOCK:
            if cached["payload"] is not None and now - cached["at"] < PLUGIN_LIBRARY_TTL:
                result = cached["payload"]
            else:
                result = run_plugins(DATA.parent / "plugins", "library", {"games": games})
                cached.update({"at": now, "payload": result})
        decorated = result.get("games", games) if isinstance(result, dict) else games
    if isinstance(decorated, list) and len(decorated) == len(games) and all(isinstance(game, dict) for game in decorated):
        games = decorated
        for index, game in enumerate(games):
            game["id"] = index
            game.setdefault("game_id", state["games"][index].get("game_id", ""))
    return {
        "games": games,
        "playlists": state.get("playlists", []),
        "filter_presets": list_presets(state),
        "ra_configured": bool(load_ra_credentials(DATA.parent)),
        "settings": public_settings(state),
        "discovery": discovery_lists(state["games"]),
        "media_epoch": MEDIA_EPOCH["value"],
    }


def _public_state_cached():
    with PUBLIC_STATE_LOCK:
        signature = _public_state_signature()
        if PUBLIC_STATE_CACHE["raw"] is not None and PUBLIC_STATE_CACHE["signature"] == signature:
            return PUBLIC_STATE_CACHE
    payload = _build_public_state()
    raw = json.dumps(payload).encode()
    with PUBLIC_STATE_LOCK:
        if PUBLIC_STATE_CACHE["raw"] is not None and PUBLIC_STATE_CACHE["signature"] == signature:
            return PUBLIC_STATE_CACHE
        PUBLIC_STATE_CACHE.update({"signature": signature, "payload": payload, "raw": raw})
        return PUBLIC_STATE_CACHE


def public_state():
    """Return the full library projection, cached until library state changes."""
    return _public_state_cached()["payload"]


def public_state_bytes():
    """Return the serialized library projection, cached until library state changes."""
    return _public_state_cached()["raw"]


def public_state_etag():
    """Stable ETag for the library projection, derived from its signature."""
    signature = _public_state_signature()
    stat = signature[0] or (0, 0, 0)
    return f'"{stat[0]:x}-{stat[1]:x}-{signature[1]}-{signature[2]}"'


def load_state_view():
    """Read-only library snapshot reused across requests until the file changes."""
    with STATE_VIEW_LOCK:
        signature = STATE_STORE.signature()
        if STATE_VIEW_CACHE["state"] is not None and STATE_VIEW_CACHE["signature"] == signature:
            return STATE_VIEW_CACHE["state"]
    state = load_state()
    with STATE_VIEW_LOCK:
        if STATE_VIEW_CACHE["state"] is not None and STATE_VIEW_CACHE["signature"] == signature:
            return STATE_VIEW_CACHE["state"]
        STATE_VIEW_CACHE.update({"signature": signature, "state": state})
        return state
def transact_state(mutator):
    """Run one read-modify-write transaction under the local and process lock."""
    with STATE_LOCK:
        result = update_state_with_result(mutator)
    with PUBLIC_STATE_LOCK:
        PUBLIC_STATE_CACHE.update({"signature": None, "payload": None, "raw": None})
    with STATE_VIEW_LOCK:
        STATE_VIEW_CACHE.update({"signature": None, "state": None})
    with PLUGIN_LIBRARY_LOCK:
        PLUGIN_LIBRARY_CACHE.update({"at": 0.0, "payload": None})
    return result


def webhook_configs(state=None):
    """Return the persisted webhook configurations list (redacted when public)."""
    state = state or load_state()
    configs = state.get("settings", {}).get("webhooks", [])
    if not isinstance(configs, list):
        return []
    return [config for config in configs if isinstance(config, dict)]
def emit_notification(*, kind="system", level="info", title="OpenBox", body="", source="", correlation_id="", dedupe_key=""):
    def mutate(state):
        return add_notification(state, kind=kind, level=level, title=title, body=body, source=source, correlation_id=correlation_id, dedupe_key=dedupe_key)
    try:
        transact_state(mutate)
    except Exception:
        LOGGER.exception("Could not persist notification")




def public_webhook_configs(state=None):
    """Return webhook configs with secrets replaced by a secret_set flag."""
    configs = []
    for config in webhook_configs(state):
        public = {
            key: value
            for key, value in config.items()
            if key != "secret"
        }
        public["secret_set"] = bool(config.get("secret"))
        configs.append(public)
    return configs


def _webhook_settings(state=None):
    state = state or load_state()
    settings = state.get("settings", {})
    return {
        "attempts": int(settings.get("webhook_attempts") or DEFAULT_ATTEMPTS),
        "timeout": int(settings.get("webhook_timeout") or DEFAULT_TIMEOUT),
    }


def _webhook_payload(envelope, configs):
    """Persist and enqueue one event envelope for matching webhook configs.

    Never raises: webhook delivery is best-effort and must not change the
    outcome of the originating operation. Returns the event id string.
    """
    event_id = str(envelope.get("id") or "")
    try:
        matched = [config for config in configs if config.get("enabled") and event_matches(config, envelope)]
        dispatcher = get_webhook_dispatcher()
        if dispatcher is None:
            return event_id
        if not dispatcher.enqueue(matched, envelope):
            LOGGER.warning("Webhook queue is full; event %s was dropped", event_id)
            _emit_webhook_failure(event_id, "Webhook delivery queue is full; the event was dropped.")
    except Exception:
        LOGGER.exception("Webhook delivery failed for event %s", event_id)
    return event_id


def event_matches(config, envelope):
    events = config.get("events") or []
    return isinstance(events, list) and str(envelope.get("type", "")) in events


def _emit_webhook_failure(event_id, error):
    """Surface a delivery failure through the Notification Center when present.

    Uses getattr so this module works even before the notification module
    lands in the same release; failures are logged when no emitter exists.
    """
    emitter = globals().get("emit_notification")
    if emitter is None:
        LOGGER.warning("Webhook event %s failed delivery: %s", event_id, error)
        return
    try:
        emitter(
            level="error",
            source="webhook",
            title="Webhook delivery failed",
            body=error,
            correlation_id=event_id,
            dedupe_key=f"webhook:{event_id}",
        )
    except Exception:
        LOGGER.exception("Failed to record webhook delivery failure notification")


def _commit_webhook_result(webhook_id, event_id, status, error, sent_at, terminal):
    """Persist the last delivery status for one webhook config.

    Runs outside every dispatcher, process, and state lock; the callback
    contract requires the worker to release all locks before invoking it.
    """
    try:
        def mutate(state):
            settings = state.setdefault("settings", {})
            for config in settings.get("webhooks", []):
                if not isinstance(config, dict):
                    continue
                if str(config.get("id") or "") != webhook_id:
                    continue
                config["last_status"] = status
                config["last_error"] = error
                if sent_at:
                    config["last_sent_at"] = sent_at
                if terminal:
                    config["last_delivery_at"] = sent_at or utc_now()
                return True
            return False

        _, updated = transact_state(mutate)
        if not updated:
            return
        if terminal and (status is None or status >= 300 or status == 0):
            _emit_webhook_failure(
                event_id,
                error or f"Webhook delivery failed with HTTP {status}." if status else (error or "Webhook delivery failed."),
            )
    except Exception:
        LOGGER.exception("Failed to commit webhook delivery status for %s", webhook_id)


def get_webhook_dispatcher():
    """Return the lazily-created dispatcher singleton, or None in safe mode.

    The dispatcher factory is replaceable under ``WEBHOOK_DISPATCHER_FACTORY``
    so handler/session tests can inject a fake without running ``main()``.
    """
    global WEBHOOK_DISPATCHER
    if os.environ.get("OPENBOX_SAFE_MODE"):
        return None
    factory = globals().get("WEBHOOK_DISPATCHER_FACTORY", _default_webhook_dispatcher_factory)
    with WEBHOOK_DISPATCHER_LOCK:
        if WEBHOOK_DISPATCHER is None:
            WEBHOOK_DISPATCHER = factory()
            WEBHOOK_DISPATCHER.start()
        return WEBHOOK_DISPATCHER


def _default_webhook_dispatcher_factory():
    from automation import WebhookDispatcher
    return WebhookDispatcher(on_result=_commit_webhook_result)


def publish_event(event, data):
    """Build and enqueue one webhook event for matching configs. Never raises.

    Returns the event id string, or "" when the event could not be built.
    """
    try:
        envelope = build_event(event, data)
    except (ValueError, TypeError) as error:
        LOGGER.warning("Skipped webhook event %s: %s", event, error)
        return ""
    try:
        configs = webhook_configs(load_state())
        _webhook_payload(envelope, configs)
    except Exception:
        LOGGER.exception("Webhook publish failed for event %s", event)
    return str(envelope.get("id") or "")


def shutdown_webhooks(wait_seconds=2.0):
    """Stop and join the lazy webhook dispatcher singleton if it exists."""
    global WEBHOOK_DISPATCHER
    with WEBHOOK_DISPATCHER_LOCK:
        dispatcher = WEBHOOK_DISPATCHER
        WEBHOOK_DISPATCHER = None
    if dispatcher is not None:
        try:
            dispatcher.shutdown(wait_seconds=wait_seconds)
        except Exception:
            LOGGER.exception("Webhook dispatcher shutdown failed")


def _publish_session_event(envelope):
    try:
        publish_event(envelope["type"], envelope["data"])
    except Exception:
        LOGGER.exception("Failed to publish session webhook event")


def session_event(kind, launch_id, game_name, exit_code=None, seconds=None):
    global EVENT_SEQUENCE
    with PROCESS_LOCK:
        EVENT_SEQUENCE += 1
        event = {
            "id": EVENT_SEQUENCE,
            "kind": kind,
            "launch_id": launch_id,
            "game": game_name,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        if exit_code is not None:
            event["exit_code"] = exit_code
        if seconds is not None:
            event["seconds"] = seconds
        SESSION_EVENTS.append(event)
        SESSION_EVENTS[:] = SESSION_EVENTS[-100:]


def resolve_library_game(state, identity, fallback_index=None):
    """Find a library game by stable ids/path, not a stale array index."""
    games = state.get("games") or []
    if not isinstance(identity, dict):
        identity = {}
    stable_id = str(identity.get("stable_game_id") or identity.get("game_id") or "").strip()
    if stable_id:
        for game in games:
            aliases = game.get("legacy_game_ids", [])
            if (
                str(game.get("game_id") or "") == stable_id
                or isinstance(aliases, list) and stable_id in {str(value) for value in aliases}
            ):
                return game
    for key in ("gameyfin_id", "steam_app_id", "heroic_app_id", "lutris_id"):
        value = str(identity.get(key) or "").strip()
        if not value:
            continue
        for game in games:
            if str(game.get(key) or "") == value:
                return game
    path = str(identity.get("game_path") or identity.get("path") or "")
    name = str(identity.get("game_name") or identity.get("game") or identity.get("name") or "")
    if path:
        matches = [game for game in games if str(game.get("path", "")) == path]
        if name:
            named = [game for game in matches if str(game.get("name", "")) == name]
            if named:
                return named[0]
        if len(matches) == 1:
            return matches[0]
    if fallback_index is not None:
        try:
            index = int(fallback_index)
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(games):
            candidate = games[index]
            if name and str(candidate.get("name", "")) != name:
                return None
            if path and str(candidate.get("path", "")) != path:
                return None
            return candidate
    return None


def game_from_payload(state, payload):
    """Resolve additive stable IDs first, then retain the numeric frontend ID."""
    if not isinstance(payload, dict):
        raise ValueError("Request payload must be an object.")
    stable_id = str(payload.get("game_id") or payload.get("stable_game_id") or "").strip()
    games = state.get("games") or []
    if stable_id:
        for game in games:
            aliases = game.get("legacy_game_ids", [])
            if (
                str(game.get("game_id") or "") == stable_id
                or isinstance(aliases, list) and stable_id in {str(value) for value in aliases}
            ):
                return game
        raise IndexError("Game not found")
    if payload.get("id") is None:
        raise ValueError("Game id is required.")
    try:
        index = int(payload["id"])
    except (TypeError, ValueError) as error:
        raise ValueError("Game id must be a number or stable game id.") from error
    if index < 0 or index >= len(games):
        raise IndexError("Game not found")
    return games[index]


def game_from_query(state, query):
    payload = {"id": query.get("id", [None])[0]}
    if query.get("game_id", [""])[0]:
        payload["game_id"] = query["game_id"][0]
    return game_from_payload(state, payload)


def finish_session(launch_id, game_index, started, process):
    with PROCESS_LOCK:
        running_snapshot = dict(RUNNING.get(launch_id, {}))
    identity = {
        "stable_game_id": running_snapshot.get("stable_game_id", ""),
        "game_path": running_snapshot.get("game_path", ""),
        "game_name": running_snapshot.get("game") or running_snapshot.get("game_name", ""),
        "steam_app_id": running_snapshot.get("steam_app_id", ""),
        "heroic_app_id": running_snapshot.get("heroic_app_id", ""),
        "lutris_id": running_snapshot.get("lutris_id", ""),
        "gameyfin_id": running_snapshot.get("gameyfin_id", ""),
    }
    state = load_state()
    with STATE_LOCK:
        settings = copy.deepcopy(state.get("settings", {}))
        game = resolve_library_game(state, identity, fallback_index=game_index) or {}
        game_snapshot = copy.deepcopy(game)
        original_game_name = str(game_snapshot.get("name", "") or identity.get("game_name") or "Untitled")
    exit_code = wait_for_exit(process, game_snapshot, settings)
    seconds = max(1, int((datetime.now() - started).total_seconds()))
    if game_snapshot:
        if settings.get("backup_on_close") and game_snapshot.get("save_paths"):
            try:
                backup_saves(game_snapshot, DATA.parent / "save-backups", label="on-close")
                enforce_backup_limit(game_snapshot, DATA.parent / "save-backups", settings.get("save_backup_limit", 10))
            except (OSError, FileNotFoundError):
                pass
        try:
            auto_attach_obs_recording(game_snapshot, started, settings)
        except (OSError, ValueError, FileNotFoundError):
            pass
        try:
            close_store_client(game_snapshot, settings)
        except (OSError, ValueError):
            pass

    session_result = {"game_name": original_game_name, "session": {}}

    def mutate(state):
        settings = state.get("settings", {})
        game = resolve_library_game(state, identity, fallback_index=game_index)
        if game is not None:
            game["playtime_seconds"] = game.get("playtime_seconds", 0) + seconds
            apply_progress_automation(game, settings)
            for key in ("video_recording", "recording", "last_recording"):
                if key in game_snapshot:
                    game[key] = game_snapshot[key]
            game_name = game.get("name", "Untitled")
        else:
            game_name = original_game_name
        session = {
            "game": game_name,
            "started": started.isoformat(timespec="seconds"),
            "seconds": seconds,
            "exit_code": exit_code,
        }
        if settings.get("track_session_history", True):
            state["history"].append(session)
            state["history"][:] = state["history"][-500:]
        session_result.update({"game_name": game_name, "session": session})
    update_state(mutate)
    game_name = session_result["game_name"]
    session = session_result["session"]
    with PROCESS_LOCK:
        running = RUNNING.pop(launch_id, {})
        PROCESSES.pop(launch_id, None)
    try:
        restore_perf_profile(str(running.get("effective_profile", "")), load_state())
    except Exception:  # never let performance tuning break session bookkeeping
        LOGGER.exception("restore_perf failed")
    session_event("stopped", launch_id, game_name, exit_code=exit_code, seconds=seconds)
    _publish_session_event(build_event("session.stopped", {
        "launch_id": launch_id,
        "game_id": running_snapshot.get("stable_game_id", ""),
        "name": game_name,
        "seconds": seconds,
        "exit_code": exit_code,
        "started_at": session.get("started", ""),
        "stopped_at": utc_now(),
    }))
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        run_plugins(DATA.parent / "plugins", "after_session", session)
    try:
        sync_cloud()
    except (OSError, ValueError):
        pass
    if running.get("restart"):
        state = load_state()
        target = resolve_library_game(state, identity, fallback_index=game_index)
        if target is not None:
            index = state["games"].index(target)
            try:
                start_game(index, stable_game_id=target.get("game_id", ""))
            except (OSError, ValueError, IndexError):
                pass


def clear_file_probe_cache():
    with FILE_PROBE_LOCK:
        FILE_PROBE_CACHE.clear()


def bump_media_epoch():
    """Invalidate browser media caches by bumping the version suffix in media URLs."""
    with MEDIA_EPOCH_LOCK:
        MEDIA_EPOCH["value"] += 1
    clear_file_probe_cache()


def download_image(url, destination):
    result = str(download_file(
        url,
        destination,
        expected_types=("image/",),
        max_bytes=32 * 1024 * 1024,
        timeout=15,
        opener=urlopen,
    ))
    bump_media_epoch()
    return result


def update_steam_metadata(game):
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
    media = DATA.parent / "media" / "steam" / app_id
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


def start_game(index=None, stable_game_id=""):
    state = load_state()
    if stable_game_id:
        selected = resolve_library_game(state, {"stable_game_id": stable_game_id}, fallback_index=index)
        if selected is None:
            raise IndexError("Game not found")
        index = state["games"].index(selected)
    elif index is None or index < 0 or index >= len(state["games"]):
        raise IndexError("Game not found")
    game = copy.deepcopy(state["games"][index])
    stable_game_id = str(game.get("game_id") or stable_game_id)
    profiles = dict(state["profiles"])
    selected_profile = str(game.get("launch_profile", "")).strip()
    if selected_profile and selected_profile in profiles:
        profiles = {game.get("platform", ""): profiles[selected_profile]}
    args, cwd = build_launch(game, profiles)
    if (
        len(args) == 1
        and not shlex.split(str(game.get("launch", "")) or "")
        and not shlex.split(str(profiles.get(game.get("platform", ""), "")) or "")
        and not os.access(str(args[0]), os.X_OK)
    ):
        raise ValueError(
            f"{game.get('name', 'This game')} has no launch command and its file is not executable. "
            "Set a launch command for the platform in Emulator profiles, or per-game in Edit game."
        )
    effective_profile = effective_profile_name(game, state["profiles"])
    apply_perf_profile(effective_profile, state)
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        result = run_plugins(DATA.parent / "plugins", "before_launch", {"game": game, "args": args, "cwd": cwd})
        if not isinstance(result, dict):
            raise ValueError("A plugin returned an invalid launch response.")
        if result.get("cancel"):
            raise ValueError(str(result.get("error") or "Launch canceled by a plugin."))
        args, cwd = result.get("args"), result.get("cwd")
    if not isinstance(args, list) or not args or not all(isinstance(part, str) and part for part in args):
        raise ValueError("A plugin returned an invalid launch command.")
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        raise ValueError("A plugin returned an invalid working directory.")
    process = subprocess.Popen(args, cwd=cwd, start_new_session=True)
    started = datetime.now()
    launch_id = secrets.token_urlsafe(8)
    stable_game_id = str(game.get("game_id") or "")
    entry = {}
    missing = {"value": False}

    def mutate(state):
        current = resolve_library_game(state, {"stable_game_id": stable_game_id}, fallback_index=index)
        if current is None:
            missing["value"] = True
            return
        current["last_played"] = started.isoformat(timespec="seconds")
        current["play_count"] = current.get("play_count", 0) + 1
        if not current.get("progress") and state.get("settings", {}).get("progress_on_first_play", "Playing"):
            current["progress"] = state.get("settings", {}).get("progress_on_first_play", "Playing")
        entry.update({
            "launch_id": launch_id,
            "game_id": index,
            "stable_game_id": stable_game_id,
            "effective_profile": effective_profile,
            "game": current.get("name", "Untitled"),
            "game_path": str(current.get("path", "")),
            "steam_app_id": str(current.get("steam_app_id") or ""),
            "heroic_app_id": str(current.get("heroic_app_id") or ""),
            "lutris_id": str(current.get("lutris_id") or ""),
            "gameyfin_id": str(current.get("gameyfin_id") or ""),
            "started": started.isoformat(timespec="seconds"),
            "pid": process.pid,
            "paused": False,
        })
    update_state(mutate)
    if missing["value"]:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        raise IndexError("Game was removed while it was launching")
    with PROCESS_LOCK:
        RUNNING[launch_id] = entry
        PROCESSES[launch_id] = process
    if is_gamescope_guest(force="--game-mode" in sys.argv) and not is_steam_launch(args):
        window_class = Path(str(args[0])).name if args else None
        threading.Thread(
            target=mark_process_windows,
            kwargs={
                "pid": process.pid,
                "app_id": steam_game_id_for(game),
                "window_name": game.get("name") or None,
                "window_class": window_class,
            },
            daemon=True,
        ).start()
    session_event("started", launch_id, entry["game"])
    _publish_session_event(build_event("session.started", {
        "launch_id": entry.get("launch_id", ""),
        "game_id": entry.get("stable_game_id", ""),
        "name": entry.get("game", "Untitled"),
        "platform": game.get("platform", ""),
        "started_at": entry.get("started", ""),
    }))
    threading.Thread(
        target=finish_session,
        args=(launch_id, index, started, process),
        daemon=True,
    ).start()
    return dict(entry)


def control_game_session(launch_id, action):
    with PROCESS_LOCK:
        process = PROCESSES.get(launch_id)
        running = RUNNING.get(launch_id)
        if not process or not running or process.poll() is not None:
            raise ValueError("That game is no longer running.")
        if action == "pause":
            os.killpg(process.pid, signal.SIGSTOP)
            running["paused"] = True
        elif action == "resume":
            os.killpg(process.pid, signal.SIGCONT)
            running["paused"] = False
        elif action in {"stop", "restart", "kill"}:
            running["restart"] = action == "restart"
            if running.get("paused") and action != "kill":
                os.killpg(process.pid, signal.SIGCONT)
            os.killpg(process.pid, signal.SIGKILL if action == "kill" else signal.SIGTERM)
        else:
            raise ValueError("Unknown session action.")
        game = running["game"]
    if action in {"pause", "resume"}:
        session_event("paused" if action == "pause" else "resumed", launch_id, game)
    return {"ok": True, "action": action}


def sync_cloud():
    state = load_state()
    folder = state.get("settings", {}).get("cloud_folder", "")
    if not folder:
        raise ValueError("Configure a mounted cloud sync folder first.")
    working_state = copy.deepcopy(state)
    result = sync_statistics(working_state, folder)

    def mutate(current):
        source_by_id = {str(game.get("game_id")): game for game in working_state.get("games", [])}
        for game in current.get("games", []):
            source = source_by_id.get(str(game.get("game_id")))
            if not source:
                continue
            for key in ("play_count", "playtime_seconds", "last_played", "progress", "rating", "favorite"):
                if key in source:
                    game[key] = source[key]
        current.setdefault("settings", {})["last_cloud_sync"] = result["synced_at"]

    update_state(mutate)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenBox/1"
    MAX_BODY = 65536
    REQUEST_TIMEOUT = 30

    def setup(self):
        super().setup()
        self.connection.settimeout(self.REQUEST_TIMEOUT)

    def log_message(self, *_):
        pass

    def send_response(self, code, message=None):
        LOGGER.debug("HTTP %s %s -> %s", getattr(self, "command", "?"), urlparse(getattr(self, "path", "")).path, code)
        super().send_response(code, message)

    def headers_common(self, content_type, cache_control="no-store"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")

    def send_bytes(self, status, data, content_type, cache_control="no-store", etag=None, last_modified=None, extra_headers=None):
        if etag and self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(304)
            self.headers_common(content_type, cache_control=cache_control)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(status)
        self.headers_common(content_type, cache_control=cache_control)
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _cache_headers(self, path, stat_result):
        etag = f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'
        last_modified = email.utils.formatdate(stat_result.st_mtime, usegmt=True)
        return etag, last_modified

    def send_file(self, status, path, content_type=None):
        path = Path(path)
        stat_result = path.stat()
        size = stat_result.st_size
        etag, last_modified = self._cache_headers(path, stat_result)
        request_cache_control = "private, max-age=31536000, immutable"
        conditional = self.headers.get("If-None-Match", "")
        if etag in {item.strip() for item in conditional.split(",")}:
            self.send_response(304)
            self.headers_common(content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream", cache_control=request_cache_control)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            return
        if not conditional:
            if_modified_since = self.headers.get("If-Modified-Since", "")
            if if_modified_since:
                try:
                    since = email.utils.parsedate_to_datetime(if_modified_since)
                    if stat_result.st_mtime < since.timestamp() + 1:
                        self.send_response(304)
                        self.headers_common(content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream", cache_control=request_cache_control)
                        self.send_header("ETag", etag)
                        self.send_header("Last-Modified", last_modified)
                        self.end_headers()
                        return
                except (TypeError, ValueError):
                    pass
        start, end = 0, size - 1
        response_status = status
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0].strip()
            if "-" not in spec:
                raise ValueError("Invalid byte range.")
            left, right = spec.split("-", 1)
            if left:
                start = int(left)
                end = int(right) if right else end
            elif right:
                length = int(right)
                start = max(0, size - length)
            if start < 0 or start >= size or end < start:
                self.send_response(416)
                self.headers_common(content_type or "application/octet-stream", cache_control=request_cache_control)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            response_status = 206
        length = max(0, end - start + 1)
        self.send_response(response_status)
        self.headers_common(content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream", cache_control=request_cache_control)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Content-Length", str(length))
        if response_status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_bytes(status, data, "application/json; charset=utf-8")

    def send_json_compressed(self, status, payload):
        """Send a JSON payload, gzipped for clients that accept it.

        Loopback bandwidth is free but compression wins on two fronts:
        large libraries make /api/library a multi-megabyte payload, and
        gzip shrinks the JSON to a fraction of that while the CPU cost is
        negligible on the local machine.
        """
        data = json.dumps(payload).encode()
        if len(data) >= GZIP_THRESHOLD and "gzip" in self.headers.get("Accept-Encoding", ""):
            compressed = gzip.compress(data)
            if len(compressed) < len(data):
                self.send_bytes(
                    status, compressed, "application/json; charset=utf-8",
                    extra_headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                )
                return
        self.send_bytes(status, data, "application/json; charset=utf-8")

    def authorized(self):
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        provided = self.headers.get("X-OpenBox-Token", "") or query_token
        return secrets.compare_digest(provided, TOKEN)

    def body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as error:
            raise ValueError("Content-Length must be a valid number.") from error
        if length < 0:
            raise ValueError("Content-Length must not be negative.")
        if length > self.MAX_BODY:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Request body was truncated.")
        return json.loads(raw or b"{}")

    def _do_GET(self):
        parsed = urlparse(self.path)
        dispatch_get(self, parsed)

    def _api_get_index(self, parsed):
        if parsed.path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_bytes()
            self.send_bytes(200, html, "text/html; charset=utf-8")
            return
    def _api_get_static(self, parsed):
        # Static UI assets (app.js/app.css) live next to index.html. Serve
        # them with long-lived caching keyed on mtime+size ETags.
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        name = Path(parsed.path).name
        if name not in {"app.js", "app.css"}:
            raise RouteNotFound("Not found")
        asset = ROOT / "static" / name
        if not asset.is_file():
            raise RouteNotFound("Not found")
        content_type = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
        self.send_file(200, asset, content_type)
        return
    def _api_get_favicon(self, parsed):
        if parsed.path in ("/favicon.svg", "/favicon.ico"):
            # Browsers request an icon on every initial load; serve the
            # repo icon instead of a 404 console error.
            icon = ROOT / "openbox.svg"
            if icon.is_file():
                self.send_bytes(200, icon.read_bytes(), "image/svg+xml")
                return
    def _api_get_api_jobs(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"jobs": JOB_MANAGER.snapshots(), "history": JOB_MANAGER.history()})
    def _api_get_api_theme_css(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        name = parse_qs(parsed.query).get("name", [""])[0]
        theme = DATA.parent / "themes" / f"{Path(name).stem}.css"
        if not name or not theme.is_file() or theme.stem != name:
            self.send_bytes(200, b"", "text/css; charset=utf-8")
            return
        theme_bytes = theme.read_bytes()
        etag = f'"{theme.stat().st_mtime_ns:x}-{len(theme_bytes):x}"'
        self.send_bytes(
            200, theme_bytes, "text/css; charset=utf-8",
            cache_control="public, max-age=0, must-revalidate",
            etag=etag,
            last_modified=email.utils.formatdate(theme.stat().st_mtime, usegmt=True),
        )
        return
    def _api_get_api_library(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        etag = public_state_etag()
        self.send_bytes(
            200, public_state_bytes(), "application/json; charset=utf-8",
            cache_control="private, no-cache", etag=etag,
        )
        return
    def _api_get_api_profiles(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        state = load_state_view()
        self.send_json(200, {"profiles": state["profiles"], "detected": discover_profiles()})
        return
    def _api_get_api_perf_profiles(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"perf_profiles": load_state_view().get("perf_profiles", {})})
        return
    def _api_get_api_settings(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, public_settings())
        return
    def _api_get_api_log(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"log": read_diagnostic_log(DATA.parent)})
        return
    def _api_get_api_diagnostic(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"report": build_report(DATA.parent)})
        return
    def _api_get_api_update(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            payload = check_update()
        except (ValueError, OSError, TypeError, AttributeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        last_checked = load_state_view().get("settings", {}).get("last_update_check", "")
        self.send_json(200, {**payload, "last_checked": last_checked})
        return
    def _api_get_api_related(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            query = parse_qs(parsed.query)
            state = load_state_view()
            index = state["games"].index(game_from_query(state, query))
            related = related_game_ids(state["games"], index)
            self.send_json(200, {"ids": related})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return
    def _api_get_api_emulators(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        emulators = emulator_status()
        with PROCESS_LOCK:
            for emulator in emulators:
                emulator["job"] = INSTALLS.get(emulator["app_id"], {})
            install_all = INSTALLS.get("__all__", {})
        self.send_json(200, {"emulators": emulators, "install_all": install_all})
        return
    def _api_get_api_saves(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            backups = [{"name": path.name, "size": path.stat().st_size} for path in list_backups(game, DATA.parent / "save-backups")]
            self.send_json(200, {"backups": backups})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return
    def _api_get_api_saves_discover(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            configured = set(game.get("save_paths", []))
            candidates = [
                item for item in discover_save_paths(game) + extra_save_candidates(game)
                if item["path"] not in configured
            ]
            self.send_json(200, {"candidates": candidates})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return
    def _api_get_api_themes(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        ensure_stock_themes(DATA.parent / "themes", ROOT)
        themes = sorted(path.stem for path in (DATA.parent / "themes").glob("*.css"))
        settings = load_state_view().get("settings", {})
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        mappings = settings.get("theme_by_platform", {})
        self.send_json(200, {
            "themes":themes,
            "selected":mappings.get(platform, settings.get("theme", "")) if platform else settings.get("theme", ""),
            "global":settings.get("theme", ""),
            "mappings":mappings,
        })
        return
    def _api_get_api_running(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            after = int(parse_qs(parsed.query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        with PROCESS_LOCK:
            payload = {
                "running": list(RUNNING.values()),
                "events": [event for event in SESSION_EVENTS if event["id"] > after],
                "last_event": EVENT_SEQUENCE,
            }
        self.send_json(200, payload)
        return
    def _api_get_api_history(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            limit = min(500, max(1, int(parse_qs(parsed.query).get("limit", ["100"])[0])))
        except ValueError:
            limit = 100
        state_view = load_state_view()
        history = list(reversed(state_view.get("history", [])[-limit:]))
        self.send_json(200, {"history": history, "enabled": state_view.get("settings", {}).get("track_session_history", True)})
        return
    def _api_get_api_ra_settings(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        credentials = load_ra_credentials(DATA.parent)
        if not credentials:
            self.send_json(200, {"configured": False})
            return
        try:
            profile = ra_api_get("API_GetUserProfile.php", {"u":credentials["username"]}, credentials)
            self.send_json(200, {
                "configured": True,
                "username": profile.get("User", credentials["username"]),
                "points": profile.get("TotalPoints", 0),
                "motto": profile.get("Motto", ""),
            })
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        return
    def _api_get_api_plugins(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"plugins":list_plugins(DATA.parent / "plugins")})
        return
    def _api_get_api_metadata_status(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        with PROCESS_LOCK:
            job = dict(METADATA_JOB)
        state_view = load_state_view()
        games = state_view["games"]
        matched = sum(bool(game.get("launchbox_db_id")) for game in games)
        def _missing(field):
            return sum(not Path(str(game.get(field) or "")).is_file() for game in games)
        coverage = {
            "games": len(games),
            "matched_games": matched,
            "matched_ratio": round(matched / len(games), 4) if games else 0.0,
        }
        for field in sorted(MEDIA_TYPES_ALL):
            coverage[f"with_{field}"] = len(games) - _missing(field)
        self.send_json(200, {"ready":METADATA_DATABASE.is_file(), "job":job, "coverage":coverage})
        return
    def _api_get_api_metadata_search(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        if not METADATA_DATABASE.is_file():
            self.send_json(409, {"error": "Download the LaunchBox metadata database first."})
            return
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            title = query.get("q", [game.get("name", "")])[0]
            results = search_games(METADATA_DATABASE, title, game.get("platform", ""))
            self.send_json(200, {"results":results})
        except (KeyError, IndexError, ValueError, sqlite3.Error) as error:
            self.send_json(400, {"error":str(error)})
        return
    def _api_get_api_media_audit(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query)
        platform = query.get("platform", ["all"])[0]
        games = [
            game for game in load_state_view()["games"]
            if platform == "all" or game.get("platform") == platform
        ]
        self.send_json(200, {
            "games":len(games),
            "matched":sum(bool(game.get("launchbox_db_id")) for game in games),
            "missing_cover":sum(not Path(str(game.get("cover") or "")).is_file() for game in games),
            "missing_background":sum(not Path(str(game.get("background") or "")).is_file() for game in games),
            "missing_screenshots":sum(not any(Path(str(path)).is_file() for path in game.get("screenshots", []) if path) for game in games),
            "missing_box_back":sum(not Path(str(game.get("box_back") or "")).is_file() for game in games),
            "missing_box_spine":sum(not Path(str(game.get("box_spine") or "")).is_file() for game in games),
            "missing_box_3d":sum(not Path(str(game.get("box_3d") or "")).is_file() for game in games),
            "missing_clear_logo":sum(not Path(str(game.get("clear_logo") or "")).is_file() for game in games),
            "missing_fanart":sum(not Path(str(game.get("fanart") or "")).is_file() for game in games),
            "missing_banner":sum(not Path(str(game.get("banner") or "")).is_file() for game in games),
            "missing_icon":sum(not Path(str(game.get("icon") or "")).is_file() for game in games),
            "missing_title_screen":sum(not Path(str(game.get("title_screen") or "")).is_file() for game in games),
            "missing_cart_front":sum(not Path(str(game.get("cart_front") or "")).is_file() for game in games),
            "missing_cart_back":sum(not Path(str(game.get("cart_back") or "")).is_file() for game in games),
            "missing_disc":sum(not Path(str(game.get("disc") or "")).is_file() for game in games),
            "missing_advertisement":sum(not Path(str(game.get("advertisement") or "")).is_file() for game in games),
            "missing_manual":sum(not Path(str(game.get("manual") or "")).is_file() for game in games),
        })
        return
    def _api_get_api_media_bulk_status(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        with PROCESS_LOCK:
            job = dict(MEDIA_JOB)
        self.send_json(200, {"job":job})
        return
    def _api_get_api_ra_badge(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query)
        name = re.sub(r"[^A-Za-z0-9_-]", "", query.get("name", [""])[0])
        locked = query.get("locked", ["0"])[0] == "1"
        if not name:
            raise BadgeNotFound("Badge not found")
            return
        badge = DATA.parent / "media/retroachievements/badges" / f"{name}{'_lock' if locked else ''}.png"
        try:
            if not badge.is_file():
                download_image(f"https://media.retroachievements.org/Badge/{badge.name}", badge)
            self.send_file(200, badge, "image/png")
        except (OSError, ValueError):
            raise BadgeNotFound("Badge not found") from None
        return
    def _api_get_api_media(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query)
        try:
            game = game_from_query(load_state_view(), query)
            kind = query["kind"][0]
            if kind == "screenshot":
                index = int(query["index"][0])
                media = Path(game.get("screenshots", [])[index])
            elif kind in {"cover", "background", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual", "video", "music", "video_snap", "video_theme", "video_trailer", "video_recording"}:
                if kind == "video":
                    _, video_path = active_video(game)
                    media = Path(video_path or game.get("video", ""))
                else:
                    media = Path(game.get(kind, ""))
            else:
                raise ValueError
            if not media.is_file():
                raise FileNotFoundError
            self.send_file(200, media)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise MediaNotFound("Media not found") from None
        return
    def _api_get_api_document(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query)
        try:
            game = game_from_query(load_state_view(), query)
            document = game.get("documents", [])[int(query["index"][0])]
            path = safe_document_file(document["path"])
            self.send_response(200)
            self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            safe_name = re.sub(r'[\r\n"]', "_", path.name)
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise DocumentNotFound("Document not found") from None
        return
    def _api_get_api_backup(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        data = json.dumps(load_state_view(), indent=2).encode()
        self.send_response(200)
        self.headers_common("application/json")
        self.send_header("Content-Disposition", "attachment; filename=openbox-library.json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return
    def _api_get_api_discovery(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, discovery_lists(load_state_view()["games"]))
        return
    def _api_get_api_related_rich(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            index = int(parse_qs(parsed.query)["id"][0])
            self.send_json(200, {"items": related_with_reasons(load_state_view()["games"], index)})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return
    def _api_get_api_emulators_recommend(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        self.send_json(200, {"recommendations": recommendations_for_platform(platform)})
        return
    def _api_get_api_emulators_dependencies(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        name = parse_qs(parsed.query).get("name", [""])[0]
        self.send_json(200, detect_dependencies(name))
        return
    def _api_get_api_media_duplicates(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"groups": find_duplicate_media(load_state_view()["games"])})
        return
    def _api_get_api_media_queue(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"queue": load_media_queue(DATA.parent / "media-queue.json")})
        return
    def _api_get_api_saves_scan(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        found = scan_all_saves(load_state_view()["games"])
        self.send_json(200, {"games": {str(key): value for key, value in found.items()}, "count": len(found)})
        return
    def _api_get_api_highscores(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            game = game_from_query(load_state(), parse_qs(parsed.query))
            self.send_json(200, {"scores": read_local_highscores(game)})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return
    def _api_get_api_obs_status(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, obs_recording_status())
        return
    def _api_get_api_platform_documents(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        docs = load_state_view().get("settings", {}).get("platform_documents", {})
        self.send_json(200, {"documents": docs.get(platform, []) if platform else docs})
        return
    def _api_get_api_platform_document(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query)
        try:
            platform = query["platform"][0]
            index = int(query["index"][0])
            document = load_state_view().get("settings", {}).get("platform_documents", {}).get(platform, [])[index]
            path = safe_document_file(document["path"])
            self.send_response(200)
            self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            safe_name = re.sub(r'[\r\n"]', "_", path.name)
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise PlatformDocumentNotFound("Platform document not found") from None
        return
    def _api_get_api_storefront_catalog(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        source = parse_qs(parsed.query).get("source", [""])[0]
        try:
            self.send_json(200, {"catalog": storefront_catalog(source, settings=load_state_view().get("settings", {}))})
        except (ValueError, OSError, FileNotFoundError, subprocess.SubprocessError) as error:
            self.send_json(400, {"error": str(error)})
        return
    def _api_get_api_gameyfin_install_status(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query)
        gameyfin_id = str(query.get("gameyfin_id", [""])[0]).strip()
        if not gameyfin_id:
            raise BadRequest("gameyfin_id is required.")
            return
        with PROCESS_LOCK:
            job = dict(INSTALLS.get(f"gameyfin:{gameyfin_id}", {"state": "idle"}))
        self.send_json(200, job)
        return
    def _api_get_api_gameyfin_providers(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            _catalog, providers = catalog_gameyfin(load_state_view().get("settings", {}))
            self.send_json(200, {"providers": providers})
        except (ValueError, OSError, TypeError, AttributeError) as error:
            self.send_json(400, {"error": str(error)})
        return
    def _api_get_api_save_tools_status(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, save_tool_status())
        return
    def _api_get_api_plugins_catalog(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"catalog": fetch_plugin_catalog()})
        return
    def _api_get_api_premium_strings(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        locale = parse_qs(parsed.query).get("locale", ["en"])[0]
        self.send_json(200, {"locale": locale, "strings": strings_for(locale)})
        return
    def _api_get_api_premium_media_packs(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"packs": list_media_packs(load_state_view().get("settings", {}))})
        return
    def _api_get_api_premium_platform_categories(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"categories": platform_categories(load_state_view().get("settings", {}))})
        return
    def _api_get_api_filter_presets(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        state = load_state()
        self.send_json(200, {
            "presets": list_presets(state),
            "bigbox_quick": bigbox_quick_presets(state),
        })
        return
    def _api_get_api_explorer_facets(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        field = parse_qs(parsed.query).get("field", ["genre"])[0]
        state = load_state_view()
        self.send_json(200, {"field": field, "facets": explorer_facets(state["games"], field)})
        return
    def _api_get_api_launcher_menu(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        payload = public_state()
        self.send_json(200, {"items": launcher_menu_items(payload["games"])})
        return
    def _api_get_api_import_exclusions(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"exclusions": list_exclusions(load_state_view())})
        return
    def _api_get_api_emulators_definitions(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"definitions": load_definitions(ROOT / "emulator_defs")})
        return
    def _api_get_api_emulators_scan_configs(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"configs": list_scan_configs(load_state_view())})
        return
    def _api_get_api_metadata_igdb_search(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        query = parse_qs(parsed.query).get("q", [""])[0]
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        try:
            results = search_igdb_games(query, platform=platform)
        except (OSError, ValueError) as error:
            self.send_json(400, {"error": str(error)})
            return
        self.send_json(200, {"results": results})
        return
    def _api_get_api_webhooks(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        state = load_state_view()
        self.send_json(200, {"webhooks": public_webhook_configs(state), "events": list(EVENT_TYPES), "attempts": int(state.get("settings", {}).get("webhook_attempts") or DEFAULT_ATTEMPTS), "timeout": int(state.get("settings", {}).get("webhook_timeout") or DEFAULT_TIMEOUT)})
        return
    def _api_get_api_queue(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"queue": resolve_queue(load_state_view())})
        return
    def _api_get_api_notifications(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        state = load_state_view()
        self.send_json(200, {"notifications": state.get("notifications", []), "unread": unread_count(state)})
        return
    def _api_get_api_tags(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"tags": tag_counts(load_state_view()["games"])})
        return
    def _api_get_api_backup_manifest(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        self.send_json(200, {"items": sorted(BACKUP_ITEMS)})
        return
    def _api_get_api_backups(self, parsed):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        folder = DATA.parent / "backups"
        backups = []
        for path in sorted(folder.glob("OpenBoxBackup-*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                with zipfile.ZipFile(path) as package:
                    manifest = json.loads(package.read("manifest.json")) if "manifest.json" in package.namelist() else {}
            except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError):
                manifest = {"items": [], "invalid": True}
            backups.append({
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "created": manifest.get("created", ""),
                "items": manifest.get("items", []),
                "invalid": bool(manifest.get("invalid")),
            })
        self.send_json(200, {"backups": backups})
        return
    def _api_post_api_launch(self, payload):
        self.launch(payload)
    def _api_post_api_session_control(self, payload):
        self.control_session(payload)
    def _api_post_api_favorite(self, payload):
        self.favorite(payload)
    def _api_post_api_game(self, payload):
        self.save_game(payload)
    def _api_post_api_game_delete(self, payload):
        self.delete_game(payload)
    def _api_post_api_games_delete_steam(self, payload):
        self.delete_steam_games(payload)
    def _api_post_api_games_bulk(self, payload):
        self.bulk_edit(payload)
    def _api_post_api_games_bulk_wizard(self, payload):
        self.bulk_wizard(payload)
    def _api_post_api_premium_media_packs_apply(self, payload):
        self.apply_media_pack_route(payload)
    def _api_post_api_queue(self, payload):
        self.queue(payload)
    def _api_post_api_notifications(self, payload):
        self.notifications(payload)
    def _api_post_api_tags(self, payload):
        self.tags(payload)
    def _api_post_api_webhooks(self, payload):
        self.save_webhooks(payload)
    def _api_post_api_webhooks_test(self, payload):
        self.test_webhook(payload)
    def _api_post_api_metadata_trailer(self, payload):
        self.download_trailer(payload)
    def _api_post_api_metadata_gog(self, payload):
        self.download_gog_route(payload)
    def _api_post_api_bigbox_mode(self, payload):
        self.bigbox_mode_switch(payload)
    def _api_post_api_import(self, payload):
        self.import_folder(payload)
    def _api_post_api_import_wizard(self, payload):
        self.import_wizard(payload)
    def _api_post_api_import_xbox360(self, payload):
        self.import_xbox360(payload)
    def _api_post_api_import_loose_arcade(self, payload):
        self.import_loose_arcade_route(payload)
    def _api_post_api_import_watch(self, payload):
        self.scan_watch_folders()
    def _api_post_api_import_steam(self, payload):
        self.import_steam_games()
    def _api_post_api_import_heroic(self, payload):
        self.import_heroic_games()
    def _api_post_api_import_lutris(self, payload):
        self.import_lutris_games()
    def _api_post_api_import_arcade(self, payload):
        self.import_arcade_games(payload)
    def _api_post_api_metadata_steam(self, payload):
        self.steam_metadata(payload)
    def _api_post_api_metadata_sync(self, payload):
        self.sync_metadata()
    def _api_post_api_metadata_apply(self, payload):
        self.apply_metadata(payload)
    def _api_post_api_media_bulk(self, payload):
        self.bulk_media(payload)
    def _api_post_api_profiles(self, payload):
        self.save_profiles(payload)
    def _api_post_api_perf_profiles(self, payload):
        self.save_perf_profiles(payload)
    def _api_post_api_settings(self, payload):
        self.save_settings(payload)
    def _api_post_api_state_recover(self, payload):
        self.recover_state(payload)
    def _api_post_api_image_group(self, payload):
        self.save_image_group(payload)
    def _api_post_api_cloud_sync(self, payload):
        self.send_json(200, sync_cloud())
    def _api_post_api_update_install(self, payload):
        update = check_update()
        self.send_json(200, install_update(update))
    def _api_post_api_desktop_install(self, payload):
        self.send_json(200, {"desktop": install_desktop_entry()})
    def _api_post_api_emulators_install(self, payload):
        self.install_emulator(payload)
    def _api_post_api_emulators_install_all(self, payload):
        self.install_all_emulators()
    def _api_post_api_emulators_update(self, payload):
        self.update_one_emulator(payload)
    def _api_post_api_emulators_update_all(self, payload):
        self.update_all_emulators_route()
    def _api_post_api_emulators_open(self, payload):
        self.open_emulator(payload)
    def _api_post_api_import_scummvm(self, payload):
        self.import_scummvm_games()
    def _api_post_api_import_rpcs3(self, payload):
        self.import_rpcs3_games()
    def _api_post_api_import_vita3k(self, payload):
        self.import_vita3k_games()
    def _api_post_api_ra_inject(self, payload):
        self.inject_ra()
    def _api_post_api_bezels_download(self, payload):
        self.download_bezels(payload)
    def _api_post_api_emumovies_settings(self, payload):
        self.save_emumovies(payload)
    def _api_post_api_emumovies_download(self, payload):
        self.emumovies_download(payload)
    def _api_post_api_media_cleanup(self, payload):
        self.cleanup_media(payload)
    def _api_post_api_screenshot(self, payload):
        self.take_screenshot(payload)
    def _api_post_api_obs_attach(self, payload):
        self.obs_attach(payload)
    def _api_post_api_saves_scan_apply(self, payload):
        self.apply_save_scan(payload)
    def _api_post_api_platform_documents(self, payload):
        self.save_platform_documents(payload)
    def _api_post_api_storefront_import(self, payload):
        self.import_storefront_catalog(payload)
    def _api_post_api_gameyfin_test(self, payload):
        self.test_gameyfin(payload)
    def _api_post_api_gameyfin_install(self, payload):
        self.install_gameyfin(payload)
    def _api_post_api_gameyfin_uninstall(self, payload):
        self.uninstall_gameyfin(payload)
    def _api_post_api_save_tools_ludusavi(self, payload):
        self.run_ludusavi_tool(payload)
    def _api_post_api_save_tools_hoard(self, payload):
        self.run_hoard_tool(payload)
    def _api_post_api_highscores_export(self, payload):
        self.export_game_highscores(payload)
    def _api_post_api_highscores_import(self, payload):
        self.import_game_highscores(payload)
    def _api_post_api_plugins_catalog_install(self, payload):
        self.install_catalog_plugin(payload)
    def _api_post_api_themes_open_folder(self, payload):
        self.open_themes_folder()
    def _api_post_api_shutdown(self, payload):
        self.shutdown(payload)
    def _api_post_api_ra_settings(self, payload):
        self.save_ra_settings(payload)
    def _api_post_api_ra_game(self, payload):
        self.ra_game(payload)
    def _api_post_api_plugins_install(self, payload):
        self.install_plugin(payload)
    def _api_post_api_plugins_toggle(self, payload):
        self.toggle_plugin(payload)
    def _api_post_api_plugins_remove(self, payload):
        self.remove_plugin(payload)
    def _api_post_api_extra_launch(self, payload):
        self.launch_extra(payload)
    def _api_post_api_saves_backup(self, payload):
        self.backup_game_saves(payload)
    def _api_post_api_saves_restore(self, payload):
        self.restore_game_saves(payload)
    def _api_post_api_saves_add(self, payload):
        self.add_game_save_path(payload)
    def _api_post_api_themes_select(self, payload):
        self.select_theme(payload)
    def _api_post_api_themes_import(self, payload):
        self.import_theme(payload)
    def _api_post_api_playlists(self, payload):
        self.save_playlist(payload)
    def _api_post_api_playlists_delete(self, payload):
        self.delete_playlist(payload)
    def _api_post_api_filter_presets(self, payload):
        self.save_filter_preset(payload)
    def _api_post_api_filter_presets_delete(self, payload):
        self.delete_filter_preset(payload)
    def _api_post_api_import_exclusions(self, payload):
        self.add_import_exclusion(payload)
    def _api_post_api_import_exclusions_delete(self, payload):
        self.remove_import_exclusion(payload)
    def _api_post_api_backup_create(self, payload):
        self.create_library_backup(payload)
    def _api_post_api_backup_restore(self, payload):
        self.restore_library_backup(payload)
    def _api_post_api_emulators_scan(self, payload):
        self.scan_emulator_folder_route(payload)
    def _api_post_api_emulators_scan_configs(self, payload):
        self.save_emulator_scan_config(payload)
    def _api_post_api_metadata_igdb_apply(self, payload):
        self.apply_igdb_metadata(payload)
    def _api_post_api_health(self, payload):
        self.health()
    def _api_post_api_health_dedupe(self, payload):
        self.dedupe()

    def _do_POST(self):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            payload = self.body()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            route = urlparse(self.path).path
            dispatch_post(self, route, payload)
        except ApiError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, json.JSONDecodeError, GameyfinError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
            LOGGER.warning("Request %s failed: %s", urlparse(self.path).path, error)
            raise BadRequest(str(error)) from None

    def _handle_request(self, method):
        path = urlparse(self.path).path
        request_id = secrets.token_hex(4)
        LOGGER.debug("HTTP %s %s started [%s]", method, path, request_id)
        try:
            getattr(self, f"_{method}")()
        except ApiError as error:
            LOGGER.info("HTTP %s %s [%s] %s: %s", method, path, request_id, error.code, error.message)
            self.send_json(error.status, error.to_payload(request_id))
        except StateCorruptError as error:
            LOGGER.error("OpenBox state is unavailable: %s", error)
            self.send_json(503, {"error": "OpenBox library data needs recovery before this operation can continue.", "code": "STATE_UNAVAILABLE", "request_id": request_id})
        except Exception:
            LOGGER.exception("Unhandled HTTP %s %s [%s]", method, path, request_id)
            self.send_json(500, {"error": "Unexpected server error. Copy the diagnostic log from Settings and include it in your report.", "code": "INTERNAL_ERROR", "request_id": request_id})

    def do_GET(self):
        self._handle_request("do_GET")

    def do_POST(self):
        self._handle_request("do_POST")

    def launch(self, payload):
        if payload.get("id") is None and not payload.get("game_id"):
            raise ValueError("Game id is required.")
        legacy_id = int(payload["id"]) if payload.get("id") is not None else int(payload.get("legacy_id", 0))
        stable_game_id = str(payload.get("game_id") or "").strip()
        if stable_game_id:
            state = load_state()
            game = game_from_payload(state, payload)
            legacy_id = state["games"].index(game)
        self.send_json(200, {"ok": True, **start_game(legacy_id, stable_game_id=stable_game_id)})

    def control_session(self, payload):
        launch_id = str(payload.get("launch_id", ""))
        action = str(payload.get("action", ""))
        self.send_json(200, control_game_session(launch_id, action))

    def favorite(self, payload):
        def mutate(state):
            game = game_from_payload(state, payload)
            game["favorite"] = not game.get("favorite", False)
            return game["favorite"]
        _, favorite = transact_state(mutate)
        self.send_json(200, {"favorite": favorite})


    def queue(self, payload):
        action = str(payload.get("action") or "list")
        def mutate(state):
            if action == "enqueue":
                enqueue_queue(state, payload.get("game_ids", []), payload.get("position"), payload.get("note", ""))
            elif action == "remove":
                remove_queue(state, payload.get("game_ids", []))
            elif action == "reorder":
                reorder_queue(state, payload.get("ordered_game_ids", []))
            elif action == "advance":
                return advance_queue(state, payload.get("current_game_id"))
            elif action not in {"list", "resolve"}:
                raise ValueError("Unknown queue action.")
            return None
        _, result = transact_state(mutate)
        self.send_json(200, {"queue": resolve_queue(load_state()), "next": result if action == "advance" else None})

    def notifications(self, payload):
        action = str(payload.get("action") or "list")
        def mutate(state):
            if action == "read":
                mark_notifications_read(state, payload.get("ids"))
            elif action == "clear":
                clear_notifications(state, payload.get("ids"))
            elif action != "list":
                raise ValueError("Unknown notification action.")
            return unread_count(state)
        _, unread = transact_state(mutate)
        state = load_state()
        self.send_json(200, {"notifications": state.get("notifications", []), "unread": unread_count(state) if action == "list" else unread})

    def tags(self, payload):
        ids = payload.get("ids")
        changes = {key: payload[key] for key in ("tags", "tags_add", "tags_remove") if key in payload}
        if not changes:
            raise ValueError("No tag changes were supplied.")
        def mutate(state):
            return bulk_update(state["games"], ids, changes)
        _, updated = transact_state(mutate)
        self.send_json(200, {"updated": updated, "tags": tag_counts(load_state()["games"])})

    def save_webhooks(self, payload):
        configs = payload.get("webhooks", payload.get("configs", []))
        if not isinstance(configs, list) or len(configs) > MAX_WEBHOOKS:
            raise ValueError(f"Webhooks must be a list of at most {MAX_WEBHOOKS} entries.")
        clean = []
        for raw in configs:
            if not isinstance(raw, dict):
                raise ValueError("Webhook configuration must be an object.")
            config = dict(raw)
            config["id"] = str(config.get("id") or f"wh-{secrets.token_hex(8)}")
            config["url"] = str(config.get("url") or "").strip()
            config["events"] = list(config.get("events") or [])
            config["enabled"] = bool(config.get("enabled", True))
            config["attempts"] = int(config.get("attempts") or DEFAULT_ATTEMPTS)
            config["timeout"] = int(config.get("timeout") or DEFAULT_TIMEOUT)
            if not config.get("secret") and raw.get("secret_set"):
                existing = next((item for item in webhook_configs() if item.get("id") == config["id"]), {})
                config["secret"] = str(existing.get("secret") or "")
            validate_webhook(config, openbox_port=self.server.server_port)
            clean.append(config)
        def mutate(state):
            settings = state.setdefault("settings", {})
            settings["webhooks"] = clean
            settings["webhook_attempts"] = int(payload.get("attempts") or DEFAULT_ATTEMPTS)
            settings["webhook_timeout"] = int(payload.get("timeout") or DEFAULT_TIMEOUT)
        transact_state(mutate)
        self.send_json(200, {"webhooks": public_webhook_configs(), "events": list(EVENT_TYPES)})

    def test_webhook(self, payload):
        config = dict(payload.get("webhook") or payload)
        result = test_ping(config, openbox_port=self.server.server_port)
        self.send_json(200, result)
    def save_game(self, payload):
        source = payload.get("game", {})
        game = {key: str(source[key]).strip() for key in FIELDS if key in source}
        game["extract_archive"] = bool(source.get("extract_archive"))
        game["hidden"] = bool(source.get("hidden"))
        for field in ("broken", "portable"):
            game[field] = bool(source.get(field))
        if "disc_count" in source:
            try:
                game["disc_count"] = max(0, int(source.get("disc_count") or 0))
            except (TypeError, ValueError) as error:
                raise ValueError("Disc count must be a number.") from error
        if game.get("progress", "") not in PROGRESS:
            raise ValueError("Unknown progress value.")
        try:
            game["rating"] = float(game.get("rating") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("Rating must be a number from 0 to 5.") from error
        if not 0 <= game["rating"] <= 5:
            raise ValueError("Rating must be between 0 and 5.")
        game["applications"] = self.clean_extras(source.get("applications", []), command=True)
        game["versions"] = self.clean_extras(source.get("versions", []), command=True)
        game["documents"] = self.clean_extras(source.get("documents", []), command=False)
        save_paths = source.get("save_paths", [])
        if not isinstance(save_paths, list):
            raise ValueError("Save paths must be a list.")
        game["save_paths"] = [str(path).strip() for path in save_paths if str(path).strip()][:50]
        screenshots = source.get("screenshots", [])
        if not isinstance(screenshots, list):
            raise ValueError("Screenshots must be a list.")
        game["screenshots"] = [str(path).strip() for path in screenshots if str(path).strip()][:100]
        if "alternate_names" in source:
            names = source.get("alternate_names", [])
            if isinstance(names, str):
                game["alternate_names"] = [name.strip() for name in names.split(";") if name.strip()]
            elif isinstance(names, list):
                game["alternate_names"] = [str(name).strip() for name in names if str(name).strip()][:20]
        normalize_video_fields(game)
        game["hide_in_bigbox"] = bool(source.get("hide_in_bigbox"))
        esrb = str(source.get("esrb", game.get("esrb", ""))).strip()
        if esrb:
            game["esrb"] = esrb
        defs = custom_field_defs(load_state().get("settings", {}))
        if "custom_fields" in source and isinstance(source.get("custom_fields"), dict):
            game["custom_fields"] = {
                str(key).strip(): str(value).strip()
                for key, value in source["custom_fields"].items()
                if str(key).strip()
            }
            normalize_custom_fields(game, defs)
        if not game.get("name"):
            raise ValueError("Name is required.")
        if not game.get("path") or not Path(game["path"]).exists():
            raise ValueError("Path must point to an existing local file.")
        def mutate(state):
            if payload.get("id") is None and not payload.get("game_id"):
                game["added_at"] = datetime.now().isoformat(timespec="seconds")
                state["games"].append(game)
            else:
                existing = game_from_payload(state, payload)
                game["game_id"] = existing.get("game_id", game.get("game_id", ""))
                existing.update(game)
        transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"ok": True})

    def bulk_edit(self, payload):
        def mutate(state):
            return bulk_update(state["games"], payload.get("ids"), payload.get("changes"))
        _, changed = transact_state(mutate)
        self.send_json(200, {"updated": changed})

    def delete_game(self, payload):
        delete_media = bool(payload.get("delete_media"))
        media_paths = []
        def mutate(state):
            game = game_from_payload(state, payload)
            if delete_media:
                media_paths.extend(game_media_paths(game))
            state["games"].remove(game)
            return game.get("name", "")
        _, removed = transact_state(mutate)
        if delete_media:
            for path in media_paths:
                try:
                    remove_file_if_safe(Path(path), DATA.parent)
                except (OSError, ValueError):
                    pass
            bump_media_epoch()
        clear_file_probe_cache()
        self.send_json(200, {"removed": removed})

    def delete_steam_games(self, payload):
        def mutate(state):
            games = state["games"]
            state["games"] = [game for game in games if str(game.get("source", "")).casefold() != "steam"]
            return len(games) - len(state["games"])
        _, removed = transact_state(mutate)
        self.send_json(200, {"removed": removed})

    @staticmethod
    def clean_extras(items, command):
        if not isinstance(items, list):
            raise ValueError("Game extras must be a list.")
        clean = []
        for item in items[:100]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            record = {"name": str(item.get("name") or Path(path).stem).strip(), "path": path}
            if command:
                record["command"] = str(item.get("command", "")).strip()
            clean.append(record)
        return clean

    def import_folder(self, payload):
        added, found, recommendations = import_folder_path(
            str(payload.get("folder", "")),
            chosen_emulators=payload.get("chosen_emulators"),
        )
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found, "recommendations": recommendations})

    def import_wizard(self, payload):
        folder = str(payload.get("folder", "")).strip()
        chosen = payload.get("chosen_emulators", {})
        if not isinstance(chosen, dict):
            raise ValueError("chosen_emulators must be an object.")
        added, found, recommendations = import_folder_path(folder, chosen_emulators=chosen)
        clear_file_probe_cache()
        installs = []
        for app_id in chosen.values():
            if not app_id:
                continue
            try:
                install_emulator(str(app_id))
                installs.append(str(app_id))
            except (OSError, ValueError, RuntimeError):
                pass
        self.send_json(200, {"added": added, "found": found, "recommendations": recommendations, "installed": installs})

    def import_xbox360(self, payload):
        imported = import_xbox360_folder(str(payload.get("folder", "")), str(payload.get("command", "")))
        added, found = merge_imported_games(imported, lambda game: ("path", game.get("path", "")))
        self.send_json(200, {"added": added, "found": found})

    def import_loose_arcade_route(self, payload):
        imported = import_loose_arcade(str(payload.get("folder", "")), str(payload.get("command", "")))
        added, found = merge_imported_games(imported, lambda game: ("path", game.get("path", "")))
        self.send_json(200, {"added": added, "found": found})

    def scan_watch_folders(self):
        folders = load_state().get("settings", {}).get("watch_folders", [])
        added = found = 0
        errors = []
        for folder in folders:
            try:
                folder_added, folder_found, _ = import_folder_path(folder)
                added += folder_added
                found += folder_found
            except (OSError, ValueError) as error:
                errors.append(str(error))
        self.send_json(200, {"added": added, "found": found, "errors": errors})

    def import_steam_games(self):
        imported = import_steam()
        def mutate(state):
            existing = {str(game.get("steam_app_id")) for game in state["games"] if game.get("steam_app_id")}
            new_games = [game for game in imported if game["steam_app_id"] not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            return len(new_games)
        _, added = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": len(imported)})

    def import_heroic_games(self):
        imported = import_heroic()
        def mutate(state):
            existing = {
                (game.get("source"), str(game.get("heroic_app_id")))
                for game in state["games"] if game.get("heroic_app_id")
            }
            new_games = [game for game in imported if (game["source"], game["heroic_app_id"]) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            return len(new_games)
        _, added = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": len(imported)})

    def import_lutris_games(self):
        imported = import_lutris()
        def mutate(state):
            existing = {str(game.get("lutris_id")) for game in state["games"] if game.get("lutris_id")}
            new_games = [game for game in imported if game["lutris_id"] not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            return len(new_games)
        _, added = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": len(imported)})

    def import_arcade_games(self, payload):
        imported = import_arcade(
            str(payload.get("folder", "")),
            str(payload.get("dat", "")),
            str(payload.get("command", "")),
            str(payload.get("source", "MAME")),
        )
        def mutate(state):
            existing = {
                (game.get("source"), str(game.get("rom_name")))
                for game in state["games"] if game.get("rom_name")
            }
            new_games = [game for game in imported if (game["source"], game["rom_name"]) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            return len(new_games)
        _, added = transact_state(mutate)
        clear_file_probe_cache()
        counts = {kind: sum(game["set_type"] == kind for game in imported) for kind in ("parent", "merged", "split", "non-merged")}
        self.send_json(200, {"added": added, "found": len(imported), "sets": counts})

    def steam_metadata(self, payload):
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        update_steam_metadata(target)
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id"), **payload})
            game.update(target)
        transact_state(mutate)
        self.send_json(200, {"ok": True})

    def sync_metadata(self):
        with PROCESS_LOCK:
            if METADATA_JOB.get("state") == "downloading":
                self.send_json(200, METADATA_JOB)
                return
            METADATA_JOB.clear()
            METADATA_JOB.update({"state":"downloading"})

        def worker():
            try:
                sync_database(METADATA_DATABASE)
                job = {"state":"done"}
            except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error) as error:
                job = {"state":"error", "error":str(error)}
            with PROCESS_LOCK:
                METADATA_JOB.clear()
                METADATA_JOB.update(job)

        JOB_MANAGER.submit("metadata", worker)
        self.send_json(202, {"state":"downloading"})

    def apply_metadata(self, payload):
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not set(media_types) <= MEDIA_TYPES_ALL:
            raise ValueError("Invalid media selection.")
        state = load_state()
        original_game = game_from_payload(state, payload)
        if "manual" in media_types and not str(original_game.get("path") or "").strip():
            raise ValueError("This game has no file path, so no manual can be imported.")
        stable_game_id = original_game.get("game_id")
        original = dict(original_game)
        updated = apply_game_metadata(
            dict(original), METADATA_DATABASE, int(payload["database_id"]), media_types,
            DATA.parent / "media/launchbox", bool(payload.get("overwrite")),
            region_priority=load_state().get("settings", {}).get("region_priority"),
        )
        notes = list(updated.pop("_media_notes") or []) if "_media_notes" in updated else []
        changes = {key:value for key,value in updated.items() if original.get(key) != value}
        def mutate(state):
            game_from_payload(state, {"game_id": stable_game_id}).update(changes)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"updated":sorted(changes), "notes":notes})

    def bulk_media(self, payload):
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not media_types or not set(media_types) <= MEDIA_TYPES_ALL:
            raise ValueError("Select at least one valid media type.")
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        platform = str(payload.get("platform", "all"))
        overwrite = bool(payload.get("overwrite"))
        with PROCESS_LOCK:
            if MEDIA_JOB.get("state") == "running":
                self.send_json(200, MEDIA_JOB)
                return
            MEDIA_JOB.clear()
            MEDIA_JOB.update({"state":"running", "current":0, "total":0, "updated":0, "errors":[]})

        def worker():
            state = load_state()
            targets = [
                (str(game.get("game_id")), str(game.get("launchbox_db_id")))
                for game in state["games"]
                if game.get("launchbox_db_id") and (platform == "all" or game.get("platform") == platform)
            ]
            with PROCESS_LOCK:
                MEDIA_JOB["total"] = len(targets)
            updated_count, errors = 0, []
            manual_missing = 0
            for current, (stable_id, database_id) in enumerate(targets, 1):
                original = {}
                try:
                    state = load_state()
                    original = dict(game_from_payload(state, {"game_id": stable_id}))
                    updated = apply_game_metadata(
                        dict(original), METADATA_DATABASE, int(database_id), media_types,
                        DATA.parent / "media/launchbox", overwrite,
                    )
                    notes = updated.pop("_media_notes") if "_media_notes" in updated else None
                    if notes:
                        manual_missing += 1
                    changes = {key:value for key,value in updated.items() if original.get(key) != value}
                    if changes:
                        def mutate(state, stable_id=stable_id, changes=changes):
                            game_from_payload(state, {"game_id": stable_id}).update(changes)
                        transact_state(mutate)
                        updated_count += 1
                except (OSError, ValueError, sqlite3.Error) as error:
                    errors.append(f"{original.get('name', stable_id)}: {error}")
                with PROCESS_LOCK:
                    MEDIA_JOB.update({"current":current, "updated":updated_count, "errors":errors[-20:], "manual_missing":manual_missing})
            bump_media_epoch()
            with PROCESS_LOCK:
                MEDIA_JOB["state"] = "done"

        JOB_MANAGER.submit("media-bulk", worker)
        self.send_json(202, {"state":"running"})

    def save_profiles(self, payload):
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict):
            raise ValueError("Profiles must be an object.")
        clean = {
            str(platform).strip(): str(command).strip()
            for platform, command in profiles.items()
            if str(platform).strip() and str(command).strip()
        }
        def mutate(state):
            state["profiles"] = clean
        transact_state(mutate)
        self.send_json(200, {"saved": len(clean)})

    def save_perf_profiles(self, payload):
        raw = payload.get("perf_profiles")
        if not isinstance(raw, dict):
            raise ValueError("Performance profiles must be an object.")
        clean = {}
        for name, entry in raw.items():
            key = str(name).strip()
            if not key or not isinstance(entry, dict):
                continue
            try:
                tdp = max(0.0, float(entry.get("tdp_w", 0)))
                restore = max(0.0, float(entry.get("restore_tdp_w", 0)))
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid TDP value for profile {key}.") from error
            if tdp or restore:
                clean[key] = {
                    "enabled": bool(entry.get("enabled", False)),
                    "tdp_w": tdp,
                    "restore_tdp_w": restore,
                }
        def mutate(state):
            state["perf_profiles"] = clean
        transact_state(mutate)
        self.send_json(200, {"saved": len(clean)})

    def recover_state(self, payload=None):
        payload = payload or {}
        if payload.get("dry_run"):
            with STATE_LOCK:
                return self.send_json(200, {
                    "dry_run": True,
                    "backup_available": STATE_STORE.backup_path.is_file(),
                    "snapshots": STATE_STORE.snapshots(),
                    "games": load_state().get("games", []) and len(load_state().get("games", [])),
                })
        if payload.get("snapshot"):
            state = STATE_STORE.restore_snapshot(str(payload["snapshot"]))
            bump_media_epoch()
            return self.send_json(200, {"ok": True, "games": len(state.get("games", [])), "snapshot": str(payload["snapshot"])})
        state = recover_library_state()
        self.send_json(200, {"ok": True, "games": len(state.get("games", []))})

    def save_settings(self, payload):
        # Hold the local state lock across the snapshot, validation, and commit
        # so a concurrent partial save cannot observe a stale settings base.
        with STATE_LOCK:
            return self._save_settings_locked(payload)

    def _save_settings_locked(self, payload):
        existing_settings = dict(load_state().get("settings", {}))
        merged = dict(existing_settings)
        for key, value in payload.items():
            if key == "gameyfin_password" and not str(value).strip():
                continue
            merged[key] = value
        folders = merged.get("watch_folders", [])
        if not isinstance(folders, list) or len(folders) > 50:
            raise ValueError("Watch folders must be a list of at most 50 paths.")
        clean_folders = []
        for value in folders:
            path = Path(str(value)).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f"Watch folder does not exist: {path}")
            if str(path) not in clean_folders:
                clean_folders.append(str(path))
        seconds = int(merged.get("screensaver_seconds", 90))
        if seconds and not 30 <= seconds <= 3600:
            raise ValueError("Screensaver delay must be 0 or between 30 and 3600 seconds.")
        mapping = merged.get("controller_map", {})
        if not isinstance(mapping, dict):
            raise ValueError("Controller mapping must be an object.")
        allowed = {"play", "back", "favorite", "random", "page_left", "page_right", "pause", "menu"}
        clean_mapping = {}
        for action, button in mapping.items():
            if action not in allowed or not isinstance(button, int) or not 0 <= button <= 31:
                raise ValueError("Controller button mappings must use buttons 0 through 31.")
            clean_mapping[action] = button
        cloud_folder = str(merged.get("cloud_folder", "")).strip()
        if cloud_folder:
            cloud_path = Path(cloud_folder).expanduser()
            if not cloud_path.is_absolute() or not cloud_path.is_dir():
                raise ValueError(f"Cloud sync folder does not exist: {cloud_path}")
            cloud_folder = str(cloud_path)
        startup_commands = clean_commands(merged.get("startup_commands", []))
        shutdown_commands = clean_commands(merged.get("shutdown_commands", []))
        track_session_history = bool(merged.get("track_session_history", True))
        backup_on_close = bool(merged.get("backup_on_close", False))
        progress_automation_enabled = bool(merged.get("progress_automation_enabled", False))
        play_minutes = int(merged.get("progress_automation_play_minutes", 30))
        idle_days = int(merged.get("progress_automation_idle_days", 30))
        if not 0 <= play_minutes <= 100000:
            raise ValueError("Progress automation play minutes must be between 0 and 100000.")
        if not 0 <= idle_days <= 3650:
            raise ValueError("Progress automation idle days must be between 0 and 3650.")
        welcome_completed = bool(merged.get("welcome_completed", False))
        image_group = str(merged.get("image_group", "cover"))
        if image_group not in {"cover", "background", "screenshot", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual"}:
            raise ValueError("Unknown default image group.")
        badge_visibility = merged.get("badge_visibility", ["favorite", "installed", "saves", "documents", "progress", "storefront", "achievements", "rating"])
        allowed_badges = {"favorite", "installed", "missing_media", "saves", "documents", "versions", "storefront", "achievements", "highscores", "progress", "rating", "broken", "portable", "controller"}
        if not isinstance(badge_visibility, list) or not set(badge_visibility) <= allowed_badges:
            raise ValueError("Badge visibility must contain known badge names.")
        save_backup_limit = int(merged.get("save_backup_limit", 10))
        if not 0 <= save_backup_limit <= 500:
            raise ValueError("Save backup limit must be between 0 and 500.")
        media_download_limit = int(merged.get("media_download_limit", 0))
        if media_download_limit < 0 or media_download_limit > 10000:
            raise ValueError("Media download limit must be between 0 and 10000.")
        auto_import_media_types = merged.get("auto_import_media_types", [])
        if not isinstance(auto_import_media_types, list) or not set(auto_import_media_types) <= MEDIA_TYPES_ALL:
            raise ValueError("Auto-import media types include an unknown media type.")
        region_priority = merged.get("region_priority", list(REGION_PRIORITY_DEFAULT))
        if not isinstance(region_priority, list) or not region_priority:
            raise ValueError("Region priority must be a non-empty list.")
        video_priority = merged.get("video_priority", ["video_snap", "video_theme", "video_trailer", "video_recording"])
        if not isinstance(video_priority, list) or not set(video_priority) <= set(["video_snap", "video_theme", "video_trailer", "video_recording", "video"]):
            raise ValueError("Invalid video priority list.")
        library_music = str(merged.get("library_music", "")).strip()
        if library_music and not Path(library_music).expanduser().is_file():
            raise ValueError("Library music path must point to an existing audio file.")
        bigbox_mode = str(merged.get("bigbox_mode", "stage"))
        if bigbox_mode not in {"stage", "hybrid", "coverflow"}:
            raise ValueError("Big Box mode must be stage, hybrid, or coverflow.")
        storefront_auto_import = merged.get("storefront_auto_import", {})
        if not isinstance(storefront_auto_import, dict):
            raise ValueError("Storefront auto-import settings must be an object.")
        clean_storefront = {
            key: bool(storefront_auto_import.get(key))
            for key in ("steam", "heroic", "lutris", "gameyfin")
        }
        obs_auto_attach = bool(merged.get("obs_auto_attach", True))
        obs_recording_path = str(merged.get("obs_recording_path", "")).strip()
        if obs_recording_path:
            recording_path = Path(obs_recording_path).expanduser()
            if not recording_path.is_absolute() or not recording_path.is_dir():
                raise ValueError(f"OBS recording folder does not exist: {recording_path}")
            obs_recording_path = str(recording_path)
        gameyfin_url = str(merged.get("gameyfin_url", "")).strip()
        if gameyfin_url and not gameyfin_url.startswith(("http://", "https://")):
            gameyfin_url = "http://" + gameyfin_url
        gameyfin_install_dir = str(merged.get("gameyfin_install_dir", "")).strip()
        if gameyfin_install_dir:
            install_path = Path(gameyfin_install_dir).expanduser()
            install_path.mkdir(parents=True, exist_ok=True)
            if not install_path.is_absolute() or not install_path.is_dir():
                raise ValueError(f"Gameyfin install folder is invalid: {install_path}")
            gameyfin_install_dir = str(install_path)
        ludusavi_backup_path = str(merged.get("ludusavi_backup_path", "")).strip()
        if ludusavi_backup_path:
            backup_path = Path(ludusavi_backup_path).expanduser()
            backup_path.mkdir(parents=True, exist_ok=True)
            ludusavi_backup_path = str(backup_path)
        hidden_sidebar_sections = merged.get("hidden_sidebar_sections", [])
        if not isinstance(hidden_sidebar_sections, list):
            raise ValueError("Hidden sidebar sections must be a list.")
        tracking_mode = str(merged.get("tracking_mode", "default")).strip().casefold()
        if tracking_mode not in TRACKING_MODES:
            raise ValueError("Unknown tracking mode.")
        tracking_delay = int(merged.get("tracking_delay", 0))
        tracking_frequency = float(merged.get("tracking_frequency", 2))
        if tracking_delay < 0 or tracking_delay > 600:
            raise ValueError("Tracking delay must be between 0 and 600 seconds.")
        if not 0.5 <= tracking_frequency <= 60:
            raise ValueError("Tracking frequency must be between 0.5 and 60 seconds.")
        progress_on_first_play = str(merged.get("progress_on_first_play", "Playing")).strip()
        if progress_on_first_play and progress_on_first_play not in PROGRESS:
            raise ValueError("Unknown progress value for first play.")
        apply_perf = str(merged.get("apply_perf", "auto")).strip().casefold()
        if apply_perf not in {"off", "auto", "always"}:
            raise ValueError("Apply performance limits must be off, auto, or always.")
        ui_window = str(merged.get("ui_window", "app")).strip().casefold()
        if ui_window not in {"app", "browser"}:
            raise ValueError("UI window mode must be app or browser.")
        gameyfin_password = str(merged.get("gameyfin_password", "")).strip()
        normalized_settings = {
                "watch_folders": clean_folders,
                "screensaver_seconds": seconds,
                "controller_map": clean_mapping,
                "badge_visibility": [str(item) for item in badge_visibility],
                "cloud_folder": cloud_folder,
                "startup_commands": startup_commands,
                "shutdown_commands": shutdown_commands,
                "track_session_history": track_session_history,
                "backup_on_close": backup_on_close,
                "save_backup_limit": save_backup_limit,
                "progress_automation_enabled": progress_automation_enabled,
                "progress_automation_play_minutes": play_minutes,
                "progress_automation_idle_days": idle_days,
                "welcome_completed": welcome_completed,
                "image_group": image_group,
                "auto_import_media_types": sorted(set(auto_import_media_types) or {"cover", "background", "screenshots"}),
                "media_download_limit": media_download_limit,
                "region_priority": [str(item) for item in region_priority],
                "video_priority": [str(item) for item in video_priority],
                "library_music": library_music,
                "video_bgm_mix": bool(merged.get("video_bgm_mix", False)),
                "bigbox_mode": bigbox_mode,
                "show_playlist_actions": bool(merged.get("show_playlist_actions", True)),
                "hidden_sidebar_sections": [str(item) for item in hidden_sidebar_sections][:20],
                "storefront_auto_import": clean_storefront,
                "obs_auto_attach": obs_auto_attach,
                "obs_recording_path": obs_recording_path,
                "dynamic_play_button": bool(merged.get("dynamic_play_button", True)),
                "custom_field_defs": custom_field_defs({"custom_field_defs": merged.get("custom_field_defs", [])}),
                "platform_categories": platform_categories({"platform_categories": merged.get("platform_categories", {})}),
                "list_columns": [str(item) for item in merged.get("list_columns", list(LIST_COLUMNS_DEFAULT))][:12],
                "library_view": str(merged.get("library_view", "grid")),
                "locale": str(merged.get("locale", "en"))[:5],
                "attract_mode_seconds": int(merged.get("attract_mode_seconds", seconds or 90)),
                "bigbox_startup_video": str(merged.get("bigbox_startup_video", "")).strip(),
                "bigbox_shutdown_commands": clean_commands(merged.get("bigbox_shutdown_commands", [])),
                "tray_enabled": bool(merged.get("tray_enabled", False)),
                "minimize_to_tray": bool(merged.get("minimize_to_tray", False)),
                "gameyfin_url": gameyfin_url,
                "gameyfin_username": str(merged.get("gameyfin_username", "")).strip(),
                "gameyfin_password": gameyfin_password,
                "gameyfin_install_dir": gameyfin_install_dir,
                "gameyfin_provider": str(merged.get("gameyfin_provider", "")).strip(),
                "ludusavi_backup_path": ludusavi_backup_path,
                "tracking_mode": tracking_mode,
                "tracking_delay": tracking_delay,
                "tracking_frequency": tracking_frequency,
                "apply_perf": apply_perf,
                "ui_window": ui_window,
                "progress_on_first_play": progress_on_first_play,
                "auto_close_store_clients": bool(merged.get("auto_close_store_clients", False)),
        }
        def mutate(state):
            settings = state.setdefault("settings", {})
            incoming_keys = {
                key for key, value in payload.items()
                if key != "gameyfin_password" or str(value).strip()
            }
            # Drop keys nobody knows about before they reach the store.
            clean_payload, dropped = sanitize_settings(payload)
            if dropped:
                LOGGER.warning("Dropping unknown settings keys: %s", ", ".join(sorted(map(str, dropped))))
            incoming_keys = {key for key in incoming_keys if key in KNOWN_SETTINGS}
            for key, value in normalized_settings.items():
                if key in incoming_keys or key not in settings:
                    settings[key] = value
        state = update_state_with_result(mutate)[0]
        self.send_json(200, public_settings(state))

    def save_image_group(self, payload):
        group = str(payload.get("group", ""))
        scope = str(payload.get("scope", "global"))
        name = str(payload.get("name", "")).strip()
        if group not in {"default", "cover", "background", "screenshot", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual"} or scope not in {"global", "platform", "playlist"}:
            raise ValueError("Unknown image group.")
        if scope != "global" and (not name or len(name) > 200):
            raise ValueError("A platform or playlist is required.")
        def mutate(state):
            settings = state.setdefault("settings", {})
            if scope == "global":
                settings["image_group"] = "cover" if group == "default" else group
            else:
                mappings = settings.setdefault(f"image_group_by_{scope}", {})
                if group == "default":
                    mappings.pop(name, None)
                else:
                    mappings[name] = group
        state = transact_state(mutate)[0]
        self.send_json(200, public_settings(state))

    def install_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
        with PROCESS_LOCK:
            if INSTALLS.get(app_id, {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS[app_id] = {"state": "installing"}

        def worker():
            try:
                profiles = install_emulator(app_id)
                def mutate(state):
                    for platform, command in profiles.items():
                        state["profiles"].setdefault(platform, command)
                transact_state(mutate)
                job = {"state": "done", "profiles": profiles}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[app_id] = job

        JOB_MANAGER.submit(f"emulator-install:{app_id}", worker)
        self.send_json(202, {"state": "installing"})

    def install_all_emulators(self):
        def worker():
            try:
                result = install_all_emulators()
                job = {"state": "done", **result}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS["__all__"] = job

        with PROCESS_LOCK:
            if INSTALLS.get("__all__", {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS["__all__"] = {"state": "installing"}
        JOB_MANAGER.submit("emulator-install-all", worker)
        self.send_json(202, {"state": "installing"})

    def open_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
        self.send_json(200, launch_emulator(app_id))

    def shutdown(self, payload):
        force = bool(payload.get("force"))
        stopped = []
        with PROCESS_LOCK:
            launch_ids = list(RUNNING.keys())
        for launch_id in launch_ids:
            try:
                control_game_session(launch_id, "kill" if force else "stop")
                stopped.append(launch_id)
            except ValueError:
                pass
        return self.send_json(200, {"stopped": len(stopped), "forced": force})

    def save_ra_settings(self, payload):
        existing = load_ra_credentials(DATA.parent)
        profile = save_ra_credentials(
            DATA.parent,
            str(payload.get("username", "")),
            str(payload.get("api_key", "") or existing.get("api_key", "")),
        )
        self.send_json(200, {
            "configured": True,
            "username": profile.get("User", ""),
            "points": profile.get("TotalPoints", 0),
            "motto": profile.get("Motto", ""),
        })

    def ra_game(self, payload):
        credentials = load_ra_credentials(DATA.parent)
        if not credentials:
            raise ValueError("Configure RetroAchievements first.")
        state = load_state()
        game = copy.deepcopy(game_from_payload(state, payload))
        stable_game_id = game.get("game_id")
        game_id, digest = match_ra_game(game, credentials, DATA.parent / "cache/retroachievements")
        def mutate(state):
            target = game_from_payload(state, {"game_id": stable_game_id})
            target["ra_game_id"] = str(game_id)
            target["ra_hash"] = digest
        transact_state(mutate)
        progress = ra_game_progress(game_id, credentials)
        progress["game_id"] = game_id
        progress = enhanced_ra_profile(progress, credentials)
        self.send_json(200, progress)

    def bulk_wizard(self, payload):
        changes = bulk_wizard_changes(payload.get("changes", {}))
        def mutate(state):
            return bulk_update(state["games"], payload.get("ids"), changes)
        _, changed = transact_state(mutate)
        self.send_json(200, {"updated": changed, "fields": list(changes.keys())})

    def apply_media_pack_route(self, payload):
        pack_id = str(payload.get("id", "")).strip()
        def mutate(state):
            return apply_media_pack(state, pack_id)
        state, pack = transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"pack": pack, "settings": public_settings(state)})

    def download_trailer(self, payload):
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        path = download_steam_trailer(target, DATA.parent / "media")
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"video_trailer": path})

    def download_gog_route(self, payload):
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        download_gog_media(target, DATA.parent / "media")
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"cover": target.get("cover", ""), "background": target.get("background", "")})

    def bigbox_mode_switch(self, payload):
        if not payload.get("entering"):
            return
        key = "bigbox_shutdown_commands"
        for command in load_state().get("settings", {}).get(key, []):
            try:
                args = shlex.split(str(command))
                args[0] = str(Path(args[0]).expanduser())
                subprocess.Popen(args, start_new_session=True)
            except (OSError, ValueError, IndexError):
                pass
        self.send_json(200, {"ok": True})

    def install_plugin(self, payload):
        manifest = install_plugin(str(payload.get("path", "")), DATA.parent / "plugins")
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"plugin":manifest})

    def toggle_plugin(self, payload):
        enabled = set_plugin_enabled(
            DATA.parent / "plugins",
            str(payload.get("id", "")),
            bool(payload.get("enabled")),
        )
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"enabled":enabled})

    def remove_plugin(self, payload):
        plugin_id = remove_plugin(DATA.parent / "plugins", str(payload.get("id", "")))
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"removed":plugin_id})

    def launch_extra(self, payload):
        state = load_state()
        game = game_from_payload(state, payload)
        kind = payload.get("kind")
        if kind not in {"applications", "versions", "documents"}:
            raise ValueError("Unknown extra type.")
        extra = game.get(kind, [])[int(payload["index"])]
        path = Path(extra["path"])
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        if kind == "documents":
            opener = shutil.which("xdg-open")
            if not opener:
                raise FileNotFoundError("xdg-open is required to open documents.")
            args = [opener, str(path)]
        elif extra.get("command"):
            args = [part.replace("{path}", str(path)) for part in shlex.split(extra["command"])]
        else:
            args = [str(path)]
        subprocess.Popen(args, cwd=str(path.parent))
        self.send_json(200, {"ok": True})

    def backup_game_saves(self, payload):
        game = game_from_payload(load_state(), payload)
        archive = backup_saves(game, DATA.parent / "save-backups")
        removed = enforce_backup_limit(game, DATA.parent / "save-backups", load_state().get("settings", {}).get("save_backup_limit", 10))
        self.send_json(200, {"backup": archive.name, "trimmed": removed})

    def restore_game_saves(self, payload):
        game = game_from_payload(load_state(), payload)
        archive = restore_saves(game, DATA.parent / "save-backups", str(payload["backup"]))
        self.send_json(200, {"restored": archive.name})

    def add_game_save_path(self, payload):
        path = Path(str(payload.get("path", ""))).expanduser()
        if not path.exists():
            raise FileNotFoundError("Save path does not exist.")
        def mutate(state):
            paths = game_from_payload(state, payload).setdefault("save_paths", [])
            if str(path) not in paths:
                paths.append(str(path))
        transact_state(mutate)
        self.send_json(200, {"path":str(path)})

    def select_theme(self, payload):
        name = str(payload.get("name", "")).strip()
        platform = str(payload.get("platform", "")).strip()
        if name and not (DATA.parent / "themes" / f"{Path(name).stem}.css").is_file():
            raise FileNotFoundError("Theme not found.")
        def mutate(state):
            settings = state.setdefault("settings", {})
            if platform:
                mappings = settings.setdefault("theme_by_platform", {})
                if name:
                    mappings[platform] = name
                else:
                    mappings.pop(platform, None)
            else:
                settings["theme"] = name
        transact_state(mutate)
        self.send_json(200, {"selected":name, "platform":platform})

    def import_theme(self, payload):
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file() or source.suffix.lower() != ".css":
            raise ValueError("Theme path must point to a CSS file.")
        destination = DATA.parent / "themes" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self.send_json(200, {"theme": destination.stem})

    def save_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        rules = payload.get("rules", {})
        if not name or not isinstance(rules, dict):
            raise ValueError("Playlist name and rules are required.")
        playlist_type = str(payload.get("type", "filter")).strip().casefold()
        if playlist_type not in {"filter", "manual"}:
            raise ValueError("Playlist type must be filter or manual.")
        state = load_state()
        clean = {
            key: str(rules.get(key, "")).strip()
            for key in ("platform", "view", "query", "platform_category", "esrb", "progress", "genre", "developer", "publisher", "installed", "hidden", "favorite")
            if str(rules.get(key, "")).strip()
        }
        members = payload.get("members", payload.get("ids", []))
        if not isinstance(members, list) or len(members) > 100000:
            raise ValueError("Playlist members must be a list.")
        member_ids = []
        for value in members:
            game = game_from_payload(state, {"game_id": value}) if str(value).startswith("game-") else game_from_payload(state, {"id": value})
            stable_id = str(game.get("game_id") or "")
            if stable_id and stable_id not in member_ids:
                member_ids.append(stable_id)
        parent = str(payload.get("parent", "")).strip()
        notes = str(payload.get("notes", "")).strip()
        def mutate(state):
            playlists = state.setdefault("playlists", [])
            existing = next((item for item in playlists if item.get("name") == name), None)
            if existing:
                existing["rules"] = clean
                existing["type"] = playlist_type
                existing["members"] = member_ids if playlist_type == "manual" else []
                existing["parent"] = parent
                existing["notes"] = notes
            else:
                playlists.append({"name": name, "type": playlist_type, "rules": clean, "members": member_ids if playlist_type == "manual" else [], "parent": parent, "notes": notes})
        transact_state(mutate)
        self.send_json(200, {"saved": name})

    def delete_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        def mutate(state):
            state["playlists"] = [item for item in state.get("playlists", []) if item.get("name") != name]
        transact_state(mutate)
        self.send_json(200, {"deleted": name})

    def save_filter_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        rules = payload.get("rules", {})
        bigbox_quick = bool(payload.get("bigbox_quick", False))
        def mutate(state):
            save_preset(state, name, rules, bigbox_quick=bigbox_quick)
        transact_state(mutate)
        self.send_json(200, {"saved": name})

    def delete_filter_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        def mutate(state):
            if not delete_preset(state, name):
                raise ValueError("Preset not found.")
        transact_state(mutate)
        self.send_json(200, {"deleted": name})

    def add_import_exclusion(self, payload):
        source = str(payload.get("source", "")).strip()
        external_id = str(payload.get("external_id", "")).strip()
        heroic_source = str(payload.get("heroic_source", "")).strip()
        def mutate(state):
            return add_exclusion(state, source, external_id, heroic_source=heroic_source)
        _, entry = transact_state(mutate)
        self.send_json(200, {"exclusion": entry})

    def remove_import_exclusion(self, payload):
        source = str(payload.get("source", "")).strip()
        external_id = str(payload.get("external_id", "")).strip()
        def mutate(state):
            remove_exclusion(state, source, external_id)
        transact_state(mutate)
        self.send_json(200, {"removed": True})

    def create_library_backup(self, payload):
        items = payload.get("items", ["library", "settings"])
        keep = int(payload.get("keep", 0))
        state = load_state()
        archive = create_backup(DATA.parent, state, items, keep=keep, running_map=RUNNING)
        self.send_json(200, {"archive": str(archive), "name": archive.name})

    def restore_library_backup(self, payload):
        archive = approved_backup_file(payload.get("path", ""))
        items = payload.get("items")
        restored = restore_backup(archive, DATA.parent, items=items, running_map=RUNNING, force=bool(payload.get("force")))
        if "media" in restored:
            bump_media_epoch()
        self.send_json(200, {"restored": restored})

    def scan_emulator_folder_route(self, payload):
        folder = str(payload.get("folder", "")).strip()
        imported = scan_emulator_folder(folder)
        added, found = merge_imported_games(imported, lambda game: ("path", str(game.get("path", ""))))
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found})

    def save_emulator_scan_config(self, payload):
        folder = str(payload.get("folder", "")).strip()
        emulator_id = str(payload.get("emulator_id", "")).strip()
        auto_update = bool(payload.get("auto_update", False))
        def mutate(state):
            return save_scan_config(state, folder, emulator_id, auto_update=auto_update)
        _, entry = transact_state(mutate)
        self.send_json(200, {"config": entry})

    def apply_igdb_metadata(self, payload):
        igdb_id = int(payload["igdb_id"])
        state = load_state()
        original = copy.deepcopy(game_from_payload(state, payload))
        stable_game_id = str(original.get("game_id") or "")
        metadata = fetch_igdb_game(igdb_id)
        def mutate(state):
            game = game_from_payload(state, {"game_id": stable_game_id})
            apply_igdb_metadata(game, metadata)
            return game.get("name", "")
        _, name = transact_state(mutate)
        self.send_json(200, {"applied": True, "game": name})

    def health(self):
        state = load_state()
        seen, duplicates, issues = {}, [], []
        for index, game in enumerate(state["games"]):
            identity = game_identity(game)
            if identity in seen:
                duplicates.append(index)
                issues.append({"id":index, "game":game.get("name", ""), "type":"Duplicate", "detail":f"Matches {state['games'][seen[identity]].get('name', '')}"})
            else:
                seen[identity] = index
            path = Path(game.get("path", ""))
            if not game.get("path") or not path.exists():
                issues.append({"id":index, "game":game.get("name", ""), "type":"Missing game", "detail":str(path)})
            if not Path(game.get("cover", "")).is_file():
                issues.append({"id":index, "game":game.get("name", ""), "type":"Missing box front", "detail":"No local cover image"})
            for kind in ("applications", "versions", "documents"):
                for extra in game.get(kind, []):
                    if not Path(extra.get("path", "")).exists():
                        issues.append({"id":index, "game":game.get("name", ""), "type":"Missing extra", "detail":extra.get("path", "")})
            for path in game.get("save_paths", []):
                if not Path(path).exists():
                    issues.append({"id":index, "game":game.get("name", ""), "type":"Missing save path", "detail":path})
            suffix = Path(game.get("path", "")).suffix.casefold()
            if suffix in {".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".iso"} and not game.get("launch") and not state["profiles"].get(game.get("platform", "")):
                issues.append({"id":index, "game":game.get("name", ""), "type":"No emulator", "detail":game.get("platform", "Unspecified")})
        self.send_json(200, {
            "games": len(state["games"]),
            "missing": sum(issue["type"] == "Missing game" for issue in issues),
            "duplicates": len(duplicates),
            "unconfigured": sum(not game.get("path") for game in state["games"]),
            "missing_media": sum(issue["type"] == "Missing box front" for issue in issues),
            "issues":issues,
        })

    def dedupe(self):
        def mutate(state):
            seen, kept, removed = set(), [], []
            for game in state["games"]:
                identity = game_identity(game)
                if identity in seen:
                    removed.append(game.get("name", ""))
                else:
                    seen.add(identity)
                    kept.append(game)
            state["games"] = kept
            return removed
        _, removed = transact_state(mutate)
        self.send_json(200, {"removed": removed})

    def _merge_imported_games(self, imported, identity_fn):
        return merge_imported_games(imported, identity_fn)

    def update_one_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
        with PROCESS_LOCK:
            key = f"update:{app_id}"
            if INSTALLS.get(key, {}).get("state") == "updating":
                self.send_json(200, {"state": "updating"})
                return
            INSTALLS[key] = {"state": "updating"}

        def worker():
            try:
                result = update_emulator(app_id)
                job = {"state": "done", **result}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[f"update:{app_id}"] = job

        JOB_MANAGER.submit(key, worker)
        self.send_json(202, {"state": "updating"})

    def update_all_emulators_route(self):
        with PROCESS_LOCK:
            if INSTALLS.get("__update_all__", {}).get("state") == "updating":
                self.send_json(200, {"state": "updating"})
                return
            INSTALLS["__update_all__"] = {"state": "updating"}

        def worker():
            try:
                result = update_all_emulators()
                job = {"state": "done", **result}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS["__update_all__"] = job

        JOB_MANAGER.submit("emulator-update-all", worker)
        self.send_json(202, {"state": "updating"})

    def import_scummvm_games(self):
        added, found = self._merge_imported_games(
            import_scummvm(),
            lambda game: ("scummvm", str(game.get("scummvm_id", ""))),
        )
        self.send_json(200, {"added": added, "found": found})

    def import_rpcs3_games(self):
        added, found = self._merge_imported_games(
            import_rpcs3_hdd(),
            lambda game: ("rpcs3", str(game.get("path", ""))),
        )
        self.send_json(200, {"added": added, "found": found})

    def import_vita3k_games(self):
        added, found = self._merge_imported_games(
            import_vita3k(),
            lambda game: ("vita3k", str(game.get("path", ""))),
        )
        self.send_json(200, {"added": added, "found": found})

    def inject_ra(self):
        credentials = load_ra_credentials(DATA.parent)
        if not credentials:
            raise ValueError("Configure RetroAchievements first.")
        self.send_json(200, inject_retroachievements(credentials))

    def download_bezels(self, payload):
        platform = str(payload.get("platform", "")).strip()
        path = download_bezel(platform, DATA.parent / "bezels")
        self.send_json(200, {"path": path})

    def save_emumovies(self, payload):
        save_emumovies_credentials(
            DATA.parent,
            str(payload.get("username", "")),
            str(payload.get("password", "")),
        )
        self.send_json(200, {"configured": True})

    def emumovies_download(self, payload):
        credentials = load_emumovies_credentials(DATA.parent)
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        path = download_emumovies_media(
            target, credentials, DATA.parent / "media", str(payload.get("type", "box")),
        )
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
            game["cover"] = path
        transact_state(mutate)
        self.send_json(200, {"path": path})

    def cleanup_media(self, payload):
        groups = find_duplicate_media(load_state()["games"], allowed_roots=[DATA.parent])
        apply = bool(payload.get("apply"))
        deleted = cleanup_duplicates(groups, dry_run=not apply, allowed_roots=[DATA.parent])
        if apply and deleted:
            bump_media_epoch()
        self.send_json(200, {"groups": len(groups), "paths": deleted, "applied": apply})

    def take_screenshot(self, payload):
        state = load_state()
        game = game_from_payload(state, payload)
        stable_game_id = game.get("game_id")
        destination = DATA.parent / "media" / "captures" / f"{Path(game.get('path', 'game')).stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        path = capture_screenshot(destination)
        def mutate(state):
            screenshots = game_from_payload(state, {"game_id": stable_game_id}).setdefault("screenshots", [])
            if path not in screenshots:
                screenshots.append(path)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"path": path})

    def obs_attach(self, payload):
        video_path = str(payload.get("path", "")).strip()
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        path = attach_recording(target, video_path)
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"path": path, "obs": obs_recording_status()})

    def apply_save_scan(self, payload):
        state = load_state()
        found = scan_all_saves(state["games"])
        found_by_id = {
            str(state["games"][index].get("game_id")): paths
            for index, paths in found.items()
            if 0 <= index < len(state["games"])
        }
        def mutate(state):
            updated = 0
            for stable_id, paths in found_by_id.items():
                try:
                    game = game_from_payload(state, {"game_id": stable_id})
                except IndexError:
                    continue
                save_paths = game.setdefault("save_paths", [])
                for path in paths:
                    if path not in save_paths:
                        save_paths.append(path)
                        updated += 1
            return updated
        _, updated = transact_state(mutate)
        self.send_json(200, {"updated": updated, "games": len(found)})

    def save_platform_documents(self, payload):
        platform = str(payload.get("platform", "")).strip()
        if not platform:
            raise ValueError("Platform is required.")
        documents = self.clean_extras(payload.get("documents", []), command=False)
        def mutate(state):
            settings = state.setdefault("settings", {})
            settings.setdefault("platform_documents", {})[platform] = documents
        transact_state(mutate)
        self.send_json(200, {"saved": platform, "count": len(documents)})

    def import_storefront_catalog(self, payload):
        source = str(payload.get("source", "")).strip()
        settings = load_state().get("settings", {})
        catalog = storefront_catalog(source, settings=settings)
        imported = catalog_entries_to_games(
            catalog,
            uninstalled_only=bool(payload.get("uninstalled_only")),
            installed_only=bool(payload.get("installed_only")),
        )
        if source.casefold() == "steam":
            def identity(game):
                return ("steam", str(game.get("steam_app_id", "")))
        elif source.casefold() == "heroic":
            def identity(game):
                return ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", "")))
        elif source.casefold() == "lutris":
            def identity(game):
                return ("lutris", str(game.get("lutris_id", "")))
        elif source.casefold() == "gameyfin":
            def identity(game):
                return ("gameyfin", str(game.get("gameyfin_id", "")))
        else:
            raise ValueError("Storefront source must be steam, heroic, lutris, or gameyfin.")
        added, found = merge_imported_games(imported, identity)
        self.send_json(200, {"added": added, "found": found, "imported": len(imported)})

    def test_gameyfin(self, payload):
        settings = dict(load_state().get("settings", {}))
        for key, value in (payload or {}).items():
            if key == "gameyfin_password" and not str(value or "").strip():
                continue
            settings[key] = value
        result = test_gameyfin_connection(settings)
        self.send_json(200, result)

    def install_gameyfin(self, payload):
        game_id = str(payload.get("gameyfin_id") or payload.get("id") or "").strip()
        if not game_id:
            raise ValueError("gameyfin_id is required.")
        library_id = payload.get("library_id")
        stable_library_id = ""
        if library_id is not None:
            try:
                library_state = load_state()
                stable_library_id = str(game_from_payload(library_state, {"id": library_id}).get("game_id") or "")
            except (ValueError, IndexError):
                stable_library_id = str(library_id)
        job_key = f"gameyfin:{game_id}"
        with PROCESS_LOCK:
            job = INSTALLS.get(job_key, {})
            if job.get("state") == "installing":
                self.send_json(200, {"state": "installing", "gameyfin_id": game_id})
                return
            INSTALLS[job_key] = {"state": "installing", "gameyfin_id": game_id}

        def worker():
            result = {"state": "error", "gameyfin_id": game_id, "error": "Install failed"}
            try:
                settings = dict(load_state().get("settings", {}))
                installed = install_gameyfin_game(settings, game_id)
                def mutate(state):
                    target = None
                    for game in state["games"]:
                        if str(game.get("gameyfin_id") or "") == game_id:
                            target = game
                            break
                    if target is None and stable_library_id:
                        target = resolve_library_game(state, {"stable_game_id": stable_library_id})
                    if target is None and library_id is not None:
                        try:
                            index = int(library_id)
                        except (TypeError, ValueError):
                            index = -1
                        if 0 <= index < len(state["games"]):
                            candidate = state["games"][index]
                            existing_id = str(candidate.get("gameyfin_id") or "")
                            if not existing_id or existing_id == game_id:
                                target = candidate
                    if target is not None:
                        target.update(installed)
                    else:
                        state["games"].append(installed)
                transact_state(mutate)
                result = {"state": "done", "gameyfin_id": game_id, "game": installed}
            except (GameyfinError, OSError, ValueError, IndexError, KeyError) as error:
                result = {"state": "error", "gameyfin_id": game_id, "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[job_key] = result

        JOB_MANAGER.submit(job_key, worker)
        self.send_json(202, {"state": "installing", "gameyfin_id": game_id})

    def uninstall_gameyfin(self, payload):
        state = load_state()
        original = game_from_payload(state, payload)
        target = copy.deepcopy(original)
        if not target.get("gameyfin_id"):
            raise ValueError("This game is not a Gameyfin entry.")
        result = uninstall_gameyfin_game(target)
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        self.send_json(200, result)

    def run_ludusavi_tool(self, payload):
        settings = load_state().get("settings", {})
        game_name = str(payload.get("name", ""))
        if "id" in payload and not game_name:
            game_name = game_from_payload(load_state(), payload).get("name", "")
        result = run_ludusavi(
            str(payload.get("action", "backup")),
            game_name=game_name,
            path=str(payload.get("path") or settings.get("ludusavi_backup_path", "")),
        )
        self.send_json(200, result)

    def run_hoard_tool(self, payload):
        game_name = str(payload.get("name", ""))
        if "id" in payload and not game_name:
            game_name = game_from_payload(load_state(), payload).get("name", "")
        result = run_hoard(str(payload.get("action", "backup")), game_name=game_name)
        self.send_json(200, result)

    def export_game_highscores(self, payload):
        state = load_state()
        game = game_from_payload(state, payload)
        export_dir = DATA.parent / "highscores" / re.sub(r"[^a-z0-9]+", "-", str(game.get("name", "game")).casefold()).strip("-")
        result = export_highscores(game, export_dir)
        self.send_json(200, result)

    def import_game_highscores(self, payload):
        import_dir = str(payload.get("path", "")).strip()
        state = load_state()
        game = game_from_payload(state, payload)
        restored = import_highscores(game, import_dir)
        self.send_json(200, {"restored": restored})

    def install_catalog_plugin(self, payload):
        catalog = fetch_plugin_catalog()
        plugin_id = str(payload.get("id", "")).strip()
        entry = next((item for item in catalog if item.get("id") == plugin_id), None)
        if not entry:
            raise ValueError("Unknown catalog plugin.")
        if entry.get("local_only"):
            raise ValueError("This catalog entry is documentation-only. Install local plugin packages manually.")
        with tempfile.TemporaryDirectory(dir=DATA.parent) as temporary:
            archive = download_plugin_package(entry, temporary)
            manifest = install_plugin(archive, DATA.parent / "plugins")
        self.send_json(200, {"plugin": manifest})

    def open_themes_folder(self):
        folder = DATA.parent / "themes"
        ensure_stock_themes(folder, ROOT)
        folder.mkdir(parents=True, exist_ok=True)
        opener = shutil.which("xdg-open")
        if not opener:
            raise FileNotFoundError("xdg-open is required to open folders.")
        subprocess.Popen([opener, str(folder)])
        self.send_json(200, {"path": str(folder)})


def main():
    bootstrap_env(DATA.parent)
    configure_logging(DATA.parent)
    LOGGER.info("OpenBox web UI starting")
    args = sys.argv[1:]
    if "--backup" in args:
        items = []
        keep = 0
        if "--items" in args:
            items = args[args.index("--items") + 1].split(",")
        if "--keep" in args:
            keep = int(args[args.index("--keep") + 1])
        state = load_state()
        archive = create_backup(DATA.parent, state, items or ["library", "settings"], keep=keep, running_map=RUNNING)
        print(archive)
        return
    if "--restore-backup" in args:
        archive = approved_backup_file(args[args.index("--restore-backup") + 1])
        restored = restore_backup(archive, DATA.parent, running_map=RUNNING)
        print(",".join(restored))
        return
    cli_code = handle_cli(args, DATA.parent)
    if cli_code is not None:
        raise SystemExit(cli_code)
    ensure_stock_themes(DATA.parent / "themes", ROOT)
    def bootstrap_state(state):
        purge_demo_games(state)
        profiles = state.setdefault("profiles", {})
        profiles.update(merge_profiles_from_definitions(profiles))
    update_state(bootstrap_state)
    WATCH_STOP.clear()
    JOB_MANAGER.submit("auto-import", auto_import_worker)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    run_configured_commands("startup_commands")
    port = server.server_address[1]
    secure_text_write(DATA.parent / "server.port", str(port))
    secure_text_write(DATA.parent / "server.token", TOKEN)
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    force_game_mode = "--game-mode" in sys.argv
    guest = is_gamescope_guest(force=force_game_mode)
    # Desktop sessions open the UI in a chrome-less app window unless the
    # ui_window setting says otherwise; flags override the setting.
    if "--app-window" in sys.argv:
        native_window = True
    elif "--no-app-window" in sys.argv:
        native_window = False
    else:
        window_mode = str(load_state().get("settings", {}).get("ui_window", "app")).strip().casefold()
        native_window = window_mode == "app" and not guest
    print(url, flush=True)
    if "--no-browser" not in sys.argv:
        opened = open_ui(url, guest=guest, force_game_mode=force_game_mode, native_window=native_window)
        browser_pid = opened.get("pid")
        if opened.get("mode") == "kiosk" and guest and browser_pid:
            browser_name = Path(str(opened.get("browser") or "")).name
            class_hint = "google-chrome" if "chrome" in browser_name.casefold() else browser_name
            threading.Thread(
                target=mark_process_windows,
                kwargs={
                    "pid": browser_pid,
                    "app_id": OPENBOX_STEAM_GAME_ID,
                    "window_class": class_hint,
                },
                daemon=True,
            ).start()
    # Run configured commands before serving: startup_commands already ran.
    def stop():
        """Graceful shutdown: stop sessions, then stop accepting requests."""
        with PROCESS_LOCK:
            launch_ids = list(RUNNING.keys())
        for launch_id in launch_ids:
            try:
                control_game_session(launch_id, "stop")
            except ValueError:
                pass
        shutdown_webhooks(wait_seconds=2.0)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        WATCH_STOP.set()
        JOB_MANAGER.cancel("auto-import")
        JOB_MANAGER.shutdown(wait=False, cancel_futures=True)
        server.server_close()
        (DATA.parent / "server.token").unlink(missing_ok=True)
        (DATA.parent / "server.port").unlink(missing_ok=True)
        run_configured_commands("shutdown_commands")


if __name__ == "__main__":
    main()
