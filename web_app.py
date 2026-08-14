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
from metadata import apply_game_metadata, batch_match, search_games, sync_database
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
from handlers.data import DataHandlers
from handlers.emulators import EmulatorsHandlers
from handlers.extensions import ExtensionsHandlers
from handlers.health import HealthHandlers
from handlers.imports import ImportsHandlers
from handlers.library import LibraryHandlers
from handlers.media import MediaHandlers
from handlers.metadata import MetadataHandlers
from handlers.sessions import SessionHandlers
from handlers.settings import SettingsHandlers

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
JOB_MANAGER.set_observer(lambda job: broadcast_event("job.finished", job))
FILE_PROBE_CACHE = {}
FILE_PROBE_LOCK = threading.Lock()
FILE_PROBE_TTL = 60.0
PLUGIN_LIBRARY_CACHE = {"at": 0.0, "payload": None}
PLUGIN_LIBRARY_TTL = 3.0
PLUGIN_LIBRARY_LOCK = threading.Lock()
MEDIA_EPOCH = {"value": 0}
MEDIA_EPOCH_LOCK = threading.Lock()
PLUGIN_EPOCH = {"value": 0}
PUBLIC_STATE_CACHE = {"signature": None, "payload": None, "raw": None, "raw_gzip": None}
PUBLIC_STATE_LOCK = threading.Lock()
STATE_VIEW_CACHE = {"signature": None, "state": None}
STATE_VIEW_LOCK = threading.Lock()
WATCH_STOP = threading.Event()
METADATA_DATABASE = DATA.parent / "metadata/launchbox.db"
SERVER_PORT = 0
WEBHOOK_DISPATCHER = None
WEBHOOK_DISPATCHER_LOCK = threading.Lock()
EVENT_SUBSCRIBERS = set()
EVENT_SUBSCRIBERS_LOCK = threading.Lock()
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
        "cover_grouping": settings.get("cover_grouping", "shape"),
        "locale": settings.get("locale", "en"),
        "strings": strings_for(settings.get("locale", "en")),
        "attract_mode_seconds": settings.get("attract_mode_seconds", settings.get("screensaver_seconds", 90)),
        "bigbox_startup_video": settings.get("bigbox_startup_video", ""),
        "bigbox_shutdown_commands": settings.get("bigbox_shutdown_commands", []),
        "tray_enabled": settings.get("tray_enabled", False),
        "minimize_to_tray": settings.get("minimize_to_tray", False),
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
    raw_gzip = gzip.compress(raw) if len(raw) >= GZIP_THRESHOLD else raw
    with PUBLIC_STATE_LOCK:
        if PUBLIC_STATE_CACHE["raw"] is not None and PUBLIC_STATE_CACHE["signature"] == signature:
            return PUBLIC_STATE_CACHE
        PUBLIC_STATE_CACHE.update({"signature": signature, "payload": payload, "raw": raw, "raw_gzip": raw_gzip})
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
def broadcast_event(kind, payload):
    """Push one event to every connected SSE subscriber. Never raises."""
    with EVENT_SUBSCRIBERS_LOCK:
        subscribers = list(EVENT_SUBSCRIBERS)
    data = json.dumps(payload, ensure_ascii=False)
    for queue in subscribers:
        try:
            queue.put((kind, data))
        except Exception:
            LOGGER.exception("SSE subscriber queue failed")


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
    broadcast_event(kind, event)


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


class Handler(LibraryHandlers, ImportsHandlers, MediaHandlers, MetadataHandlers, SessionHandlers, SettingsHandlers, ExtensionsHandlers, HealthHandlers, EmulatorsHandlers, DataHandlers, BaseHTTPRequestHandler):
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
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.bunny.net; font-src 'self' https://fonts.bunny.net; script-src 'self' 'unsafe-inline'")

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
            html = (ROOT / "index.html").read_bytes().decode("utf-8")
            # <script src> and <link> loads cannot carry the X-OpenBox-Token
            # header, so embed the token in the static asset URLs; the
            # authorized() check accepts the query form.
            html = html.replace(
                '"/static/app.js"', f'"/static/app.js?token={TOKEN}"',
            ).replace(
                '"/static/app.css"', f'"/static/app.css?token={TOKEN}"',
            ).replace(
                '"/static/logo.png"', f'"/static/logo.png?token={TOKEN}"',
            )
            self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
    def _api_get_static(self, parsed):
        # Static UI assets (app.js/app.css) live next to index.html. Serve
        # them with long-lived caching keyed on mtime+size ETags.
        name = Path(parsed.path).name
        if name not in {"app.js", "app.css", "logo.png"}:
            raise RouteNotFound("Not found")
        if name == "logo.png":
            asset = ROOT / "assets" / "openbox-logo.png"
            if asset.is_file():
                self.send_file(200, asset, "image/png")
                return
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

    def _api_get_api_events(self, parsed):
        queue = __import__("queue").Queue()
        with EVENT_SUBSCRIBERS_LOCK:
            EVENT_SUBSCRIBERS.add(queue)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    kind, data = queue.get(timeout=15)
                except Exception:
                    # Keep the connection alive with a heartbeat comment.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with EVENT_SUBSCRIBERS_LOCK:
                EVENT_SUBSCRIBERS.discard(queue)

    def _do_POST(self):
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
    _settings = load_state().get("settings", {})
    secure_text_write(
        DATA.parent / "native-host-flags",
        f"{int(bool(_settings.get('tray_enabled')))} {int(bool(_settings.get('minimize_to_tray')))}\n",
    )
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    force_game_mode = "--game-mode" in sys.argv
    guest = is_gamescope_guest(force=force_game_mode)
    # Desktop sessions open the UI in a chrome-less app window by default;
    # flags override.
    if "--app-window" in sys.argv:
        native_window = True
    elif "--no-app-window" in sys.argv:
        native_window = False
    else:
        native_window = not guest
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

    def request_shutdown(_signum, _frame):
        # Serve_forever is running in this thread; raise so the finally block
        # runs the graceful teardown. The host sends SIGTERM after POSTing
        # /api/shutdown, so this is the backstop, not the primary path.
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    previous_sigint = signal.signal(signal.SIGINT, request_shutdown)
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
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    main()
