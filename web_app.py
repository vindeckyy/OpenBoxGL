#!/usr/bin/env python3
"""Local browser UI for OpenBox. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC."""

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
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse

from arcade import import_arcade
from catalog import PROGRESS, apply_progress_automation, bulk_update, game_media_paths, related_game_ids
from cloud_sync import sync_statistics
from emulators import emulator_status, install_all_emulators, install_emulator, launch_emulator, recommendations_for_platform, update_all_emulators, update_emulator
from importers import import_heroic, import_lutris, import_steam
from metadata import apply_game_metadata, search_games, sync_database
from openbox_logging import configure_logging, read_diagnostic_log
from openbox import DATA, EXTENSIONS, PLATFORM_BY_EXTENSION, build_launch, discover_profiles, load_state, purge_demo_games, save_state
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
    save_media_queue,
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
    preset_by_name,
    save_preset,
)
from parity_deeplinks import handle_cli, launcher_menu_items, parse_uri
from parity_gamescope import (
    OPENBOX_STEAM_GAME_ID,
    is_gamescope_guest,
    is_steam_launch,
    mark_process_windows,
    open_ui,
    steam_game_id_for,
)
from parity_backup import BACKUP_ITEMS, create_backup, restore_backup
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
    BULK_WIZARD_FIELDS,
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
WATCH_STOP = threading.Event()
METADATA_DATABASE = DATA.parent / "metadata/launchbox.db"
FIELDS = {
    "name", "platform", "genre", "year", "developer", "publisher", "series",
    "collection", "description", "path", "launch", "cover", "background",
    "source", "steam_app_id", "lutris_id", "install_dir",
    "heroic_app_id", "rom_name", "clone_of", "set_type", "ra_game_id", "ra_hash", "launchbox_db_id", "archive_member", "video", "music",
    "video_snap", "video_theme", "video_trailer", "video_recording",
    "progress", "rating", "notes", "region", "play_mode", "sort_title", "added_at",
    "alternate_names", "max_players", "wikipedia_url", "video_url", "hide_in_bigbox", "esrb",
    "gameyfin_id", "gameyfin_provider", "store_catalog", "store_installed", "owned",
    "tracking_mode", "tracking_delay", "tracking_frequency", "tracking_process_name", "igdb_id",
}


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
    with STATE_LOCK:
        state = load_state()
        existing = {game.get("path") for game in state["games"]}
        settings = state.get("settings", {})
        additions = []
        if not recommend:
            recommendations = {}
        for item in candidates:
            if item["path"] in existing:
                continue
            normalize_video_fields(item)
            additions.append(item)
            if recommend:
                platform = item.get("platform", "")
                recommendations.setdefault(platform, recommend_emulators(platform))
        if additions:
            state["games"].extend(additions)
            save_state(state)
            media_types = media_types_from_settings(settings)
            limit = int(settings.get("media_download_limit", 0) or 0)
            queue_path = DATA.parent / "media-queue.json"
            queued = 0
            for game in additions:
                if limit and queued >= limit:
                    break
                if game.get("launchbox_db_id"):
                    enqueue_media_job(queue_path, {
                        "name": game.get("name"),
                        "path": game.get("path"),
                        "media": sorted(media_types),
                    })
                    queued += 1
    return len(additions), len(candidates), recommendations


def merge_imported_games(imported, identity_fn):
    with STATE_LOCK:
        state = load_state()
        imported = filter_imported(imported, state)
        existing = {identity_fn(game) for game in state["games"]}
        new_games = [game for game in imported if identity_fn(game) not in existing]
        timestamp = datetime.now().isoformat(timespec="seconds")
        default_progress = state.get("settings", {}).get("progress_on_first_play", "Playing")
        for game in new_games:
            game["added_at"] = timestamp
            if default_progress and not game.get("progress"):
                game["progress"] = default_progress
            normalize_video_fields(game)
        state["games"].extend(new_games)
        save_state(state)
    return len(new_games), len(imported)


def auto_import_worker():
    while not WATCH_STOP.wait(10):
        state = load_state()
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
        "media_packs": list_media_packs(settings),
        "controller_prompt_hint": settings.get("controller_prompt_hint", ""),
        "premium_features_free": True,
        "progress_on_first_play": settings.get("progress_on_first_play", "Playing"),
        "tracking_mode": settings.get("tracking_mode", "default"),
        "tracking_delay": settings.get("tracking_delay", 0),
        "tracking_frequency": settings.get("tracking_frequency", 2),
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


def public_state():
    with STATE_LOCK:
        state = load_state()
    save_indices = set(games_with_saves(state["games"]))
    games = []
    for index, game in enumerate(state["games"]):
        normalize_video_fields(game)
        visible = {key: game.get(key, "") for key in FIELDS}
        video_field, video_path = active_video(game, state.get("settings", {}).get("video_priority"))
        path_exists = bool(game.get("path")) and Path(game["path"]).exists()
        store_installed = bool(game["store_installed"]) if "store_installed" in game else path_exists
        visible.update({
            "id": index,
            "favorite": bool(game.get("favorite")),
            "hidden": bool(game.get("hidden")),
            "hide_in_bigbox": bool(game.get("hide_in_bigbox")),
            "last_played": game.get("last_played", ""),
            "play_count": game.get("play_count", 0),
            "playtime_seconds": game.get("playtime_seconds", 0),
            "path_exists": path_exists,
            "has_cover": bool(game.get("cover")) and Path(game["cover"]).is_file(),
            "has_background": bool(game.get("background")) and Path(game["background"]).is_file(),
            "has_video": bool(video_path),
            "active_video_field": video_field,
            "has_music": bool(game.get("music")) and Path(game["music"]).is_file(),
            "has_saves": index in save_indices or bool(game.get("save_paths")),
            "has_documents": bool(game.get("documents")),
            "extract_archive": bool(game.get("extract_archive")),
            "applications": game.get("applications", []),
            "versions": game.get("versions", []),
            "documents": game.get("documents", []),
            "save_paths": game.get("save_paths", []),
            "screenshots": game.get("screenshots", []),
            "alternate_names": game.get("alternate_names", []) if isinstance(game.get("alternate_names"), list) else [name for name in str(game.get("alternate_names") or "").split(";") if name.strip()],
            "available_screenshots": [
                index for index, path in enumerate(game.get("screenshots", []))
                if Path(path).is_file()
            ],
            "esrb": game.get("esrb", ""),
            "custom_fields": game.get("custom_fields", {}) if isinstance(game.get("custom_fields"), dict) else {},
            "platform_category": category_for_platform(game.get("platform", ""), state.get("settings", {})),
            "store_catalog": bool(game.get("store_catalog")),
            "store_installed": store_installed,
            "owned": bool(game.get("owned") or game.get("store_catalog") or game.get("steam_app_id") or game.get("heroic_app_id") or game.get("lutris_id") or game.get("gameyfin_id")),
            "installable": bool(game.get("gameyfin_id")) and not store_installed,
            "gameyfin_id": game.get("gameyfin_id", ""),
        })
        games.append(visible)
    decorated = games
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        result = run_plugins(DATA.parent / "plugins", "library", {"games": games})
        decorated = result.get("games", games) if isinstance(result, dict) else games
    if isinstance(decorated, list) and len(decorated) == len(games) and all(isinstance(game, dict) for game in decorated):
        games = decorated
        for index, game in enumerate(games):
            game["id"] = index
    return {
        "games": games,
        "playlists": state.get("playlists", []),
        "filter_presets": list_presets(state),
        "ra_configured": bool(load_ra_credentials(DATA.parent)),
        "settings": public_settings(state),
        "discovery": discovery_lists(state["games"]),
    }


def session_event(kind, launch_id, game_name):
    global EVENT_SEQUENCE
    with PROCESS_LOCK:
        EVENT_SEQUENCE += 1
        SESSION_EVENTS.append({
            "id": EVENT_SEQUENCE,
            "kind": kind,
            "launch_id": launch_id,
            "game": game_name,
            "time": datetime.now().isoformat(timespec="seconds"),
        })
        SESSION_EVENTS[:] = SESSION_EVENTS[-100:]


def resolve_library_game(state, identity, fallback_index=None):
    """Find a library game by stable ids/path, not a stale array index."""
    games = state.get("games") or []
    if not isinstance(identity, dict):
        identity = {}
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


def finish_session(launch_id, game_index, started, process):
    with PROCESS_LOCK:
        running_snapshot = dict(RUNNING.get(launch_id, {}))
    identity = {
        "game_path": running_snapshot.get("game_path", ""),
        "game_name": running_snapshot.get("game") or running_snapshot.get("game_name", ""),
        "steam_app_id": running_snapshot.get("steam_app_id", ""),
        "heroic_app_id": running_snapshot.get("heroic_app_id", ""),
        "lutris_id": running_snapshot.get("lutris_id", ""),
        "gameyfin_id": running_snapshot.get("gameyfin_id", ""),
    }
    with STATE_LOCK:
        state = load_state()
        settings = state.get("settings", {})
        game = resolve_library_game(state, identity, fallback_index=game_index) or {}
        game_path = str(game.get("path", "") or identity.get("game_path", ""))
        original_game_name = str(game.get("name", "") or identity.get("game_name") or "Untitled")
    exit_code = wait_for_exit(process, game, settings)
    seconds = max(1, int((datetime.now() - started).total_seconds()))
    with STATE_LOCK:
        state = load_state()
        settings = state.get("settings", {})
        game = resolve_library_game(state, identity, fallback_index=game_index)
        if game is not None:
            game["playtime_seconds"] = game.get("playtime_seconds", 0) + seconds
            apply_progress_automation(game, settings)
            if settings.get("backup_on_close") and game.get("save_paths"):
                try:
                    backup_saves(game, DATA.parent / "save-backups", label="on-close")
                    enforce_backup_limit(game, DATA.parent / "save-backups", settings.get("save_backup_limit", 10))
                except (OSError, FileNotFoundError):
                    pass
            try:
                auto_attach_obs_recording(game, started, settings)
            except (OSError, ValueError, FileNotFoundError):
                pass
            game_name = game.get("name", "Untitled")
            close_store_client(game, settings)
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
        save_state(state)
    with PROCESS_LOCK:
        running = RUNNING.pop(launch_id, {})
        PROCESSES.pop(launch_id, None)
    session_event("stopped", launch_id, game_name)
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        run_plugins(DATA.parent / "plugins", "after_session", session)
    try:
        sync_cloud()
    except (OSError, ValueError):
        pass
    if running.get("restart"):
        state = load_state()
        index = next(
            (
                index for index, game in enumerate(state["games"])
                if game.get("path") == game_path and game.get("name") == original_game_name
            ),
            None,
        )
        if index is not None:
            try:
                start_game(index)
            except (OSError, ValueError, IndexError):
                pass


def download_image(url, destination):
    request = Request(url, headers={"User-Agent": "OpenBox/1"})
    with urlopen(request, timeout=15) as response:
        if not response.headers.get_content_type().startswith("image/"):
            raise ValueError("The media server did not return an image.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.read())
    return str(destination)


def update_steam_metadata(game):
    app_id = str(game.get("steam_app_id", ""))
    if not app_id.isdigit():
        raise ValueError("This game has no Steam App ID.")
    request = Request(
        f"https://store.steampowered.com/api/appdetails?appids={app_id}",
        headers={"User-Agent": "OpenBox/1"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
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


def start_game(index):
    with STATE_LOCK:
        state = load_state()
        game = state["games"][index]
        args, cwd = build_launch(game, state["profiles"])
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
        game["last_played"] = started.isoformat(timespec="seconds")
        game["play_count"] = game.get("play_count", 0) + 1
        if not game.get("progress") and state.get("settings", {}).get("progress_on_first_play", "Playing"):
            game["progress"] = state.get("settings", {}).get("progress_on_first_play", "Playing")
        save_state(state)
        entry = {
            "launch_id": launch_id,
            "game_id": index,
            "game": game.get("name", "Untitled"),
            "game_path": str(game.get("path", "")),
            "steam_app_id": str(game.get("steam_app_id") or ""),
            "heroic_app_id": str(game.get("heroic_app_id") or ""),
            "lutris_id": str(game.get("lutris_id") or ""),
            "gameyfin_id": str(game.get("gameyfin_id") or ""),
            "started": started.isoformat(timespec="seconds"),
            "pid": process.pid,
            "paused": False,
        }
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
    with STATE_LOCK:
        state = load_state()
        folder = state.get("settings", {}).get("cloud_folder", "")
        if not folder:
            raise ValueError("Configure a mounted cloud sync folder first.")
        result = sync_statistics(state, folder)
        save_state(state)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenBox/1"

    def log_message(self, *_):
        pass

    def send_response(self, code, message=None):
        LOGGER.debug("HTTP %s %s -> %s", getattr(self, "command", "?"), urlparse(getattr(self, "path", "")).path, code)
        super().send_response(code, message)

    def headers_common(self, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.headers_common(content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def authorized(self):
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        provided = self.headers.get("X-OpenBox-Token", "") or query_token
        return secrets.compare_digest(provided, TOKEN)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def _do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_bytes()
            self.send_bytes(200, html, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/theme.css":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            name = parse_qs(parsed.query).get("name", [""])[0]
            theme = DATA.parent / "themes" / f"{Path(name).stem}.css"
            if not name or not theme.is_file() or theme.stem != name:
                self.send_bytes(200, b"", "text/css; charset=utf-8")
                return
            self.send_bytes(200, theme.read_bytes(), "text/css; charset=utf-8")
            return
        if parsed.path == "/api/library":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, public_state())
            return
        if parsed.path == "/api/profiles":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            state = load_state()
            self.send_json(200, {"profiles": state["profiles"], "detected": discover_profiles()})
            return
        if parsed.path == "/api/settings":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, public_settings())
            return
        if parsed.path == "/api/log":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"log": read_diagnostic_log(DATA.parent)})
            return
        if parsed.path == "/api/update":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                self.send_json(200, check_update())
            except (ValueError, OSError, TypeError, AttributeError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if parsed.path == "/api/related":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                index = int(parse_qs(parsed.query)["id"][0])
                games = load_state()["games"]
                related = related_game_ids(games, index)
                self.send_json(200, {"ids": related})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/emulators":
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
        if parsed.path == "/api/saves":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                game = load_state()["games"][int(parse_qs(parsed.query)["id"][0])]
                backups = [{"name": path.name, "size": path.stat().st_size} for path in list_backups(game, DATA.parent / "save-backups")]
                self.send_json(200, {"backups": backups})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/saves/discover":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                game = load_state()["games"][int(parse_qs(parsed.query)["id"][0])]
                configured = set(game.get("save_paths", []))
                candidates = [
                    item for item in discover_save_paths(game) + extra_save_candidates(game)
                    if item["path"] not in configured
                ]
                self.send_json(200, {"candidates": candidates})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/themes":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            ensure_stock_themes(DATA.parent / "themes", ROOT)
            themes = sorted(path.stem for path in (DATA.parent / "themes").glob("*.css"))
            settings = load_state().get("settings", {})
            platform = parse_qs(parsed.query).get("platform", [""])[0]
            mappings = settings.get("theme_by_platform", {})
            self.send_json(200, {
                "themes":themes,
                "selected":mappings.get(platform, settings.get("theme", "")) if platform else settings.get("theme", ""),
                "global":settings.get("theme", ""),
                "mappings":mappings,
            })
            return
        if parsed.path == "/api/running":
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
        if parsed.path == "/api/history":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                limit = min(500, max(1, int(parse_qs(parsed.query).get("limit", ["100"])[0])))
            except ValueError:
                limit = 100
            history = list(reversed(load_state().get("history", [])[-limit:]))
            self.send_json(200, {"history": history, "enabled": load_state().get("settings", {}).get("track_session_history", True)})
            return
        if parsed.path == "/api/ra/settings":
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
        if parsed.path == "/api/plugins":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"plugins":list_plugins(DATA.parent / "plugins")})
            return
        if parsed.path == "/api/metadata/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            with PROCESS_LOCK:
                job = dict(METADATA_JOB)
            self.send_json(200, {"ready":METADATA_DATABASE.is_file(), "job":job})
            return
        if parsed.path == "/api/metadata/search":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            if not METADATA_DATABASE.is_file():
                self.send_json(409, {"error": "Download the LaunchBox metadata database first."})
                return
            try:
                query = parse_qs(parsed.query)
                game = load_state()["games"][int(query["id"][0])]
                title = query.get("q", [game.get("name", "")])[0]
                results = search_games(METADATA_DATABASE, title, game.get("platform", ""))
                self.send_json(200, {"results":results})
            except (KeyError, IndexError, ValueError, sqlite3.Error) as error:
                self.send_json(400, {"error":str(error)})
            return
        if parsed.path == "/api/media/audit":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            platform = query.get("platform", ["all"])[0]
            games = [
                game for game in load_state()["games"]
                if platform == "all" or game.get("platform") == platform
            ]
            self.send_json(200, {
                "games":len(games),
                "matched":sum(bool(game.get("launchbox_db_id")) for game in games),
                "missing_cover":sum(not Path(game.get("cover", "")).is_file() for game in games),
                "missing_background":sum(not Path(game.get("background", "")).is_file() for game in games),
                "missing_screenshots":sum(not any(Path(path).is_file() for path in game.get("screenshots", [])) for game in games),
            })
            return
        if parsed.path == "/api/media/bulk/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            with PROCESS_LOCK:
                job = dict(MEDIA_JOB)
            self.send_json(200, {"job":job})
            return
        if parsed.path == "/api/ra/badge":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            name = re.sub(r"[^A-Za-z0-9_-]", "", query.get("name", [""])[0])
            locked = query.get("locked", ["0"])[0] == "1"
            if not name:
                self.send_json(404, {"error": "Badge not found"})
                return
            badge = DATA.parent / "media/retroachievements/badges" / f"{name}{'_lock' if locked else ''}.png"
            try:
                if not badge.is_file():
                    download_image(f"https://media.retroachievements.org/Badge/{badge.name}", badge)
                self.send_bytes(200, badge.read_bytes(), "image/png")
            except (OSError, ValueError):
                self.send_json(404, {"error": "Badge not found"})
            return
        if parsed.path == "/api/media":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            try:
                game = load_state()["games"][int(query["id"][0])]
                kind = query["kind"][0]
                if kind == "screenshot":
                    index = int(query["index"][0])
                    media = Path(game.get("screenshots", [])[index])
                elif kind in {"cover", "background", "video", "music", "video_snap", "video_theme", "video_trailer", "video_recording"}:
                    if kind == "video":
                        _, video_path = active_video(game)
                        media = Path(video_path or game.get("video", ""))
                    else:
                        media = Path(game.get(kind, ""))
                else:
                    raise ValueError
                if not media.is_file():
                    raise FileNotFoundError
                self.send_bytes(200, media.read_bytes(), mimetypes.guess_type(media.name)[0] or "application/octet-stream")
            except (KeyError, IndexError, ValueError, FileNotFoundError):
                self.send_json(404, {"error": "Media not found"})
            return
        if parsed.path == "/api/document":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            try:
                game = load_state()["games"][int(query["id"][0])]
                document = game.get("documents", [])[int(query["index"][0])]
                path = Path(document["path"])
                if not path.is_file():
                    raise FileNotFoundError
                self.send_response(200)
                self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                safe_name = re.sub(r'[\r\n"]', "_", path.name)
                self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)
            except (KeyError, IndexError, ValueError, FileNotFoundError):
                self.send_json(404, {"error": "Document not found"})
            return
        if parsed.path == "/api/backup":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            data = json.dumps(load_state(), indent=2).encode()
            self.send_response(200)
            self.headers_common("application/json")
            self.send_header("Content-Disposition", "attachment; filename=openbox-library.json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/discovery":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, discovery_lists(load_state()["games"]))
            return
        if parsed.path == "/api/related/rich":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                index = int(parse_qs(parsed.query)["id"][0])
                self.send_json(200, {"items": related_with_reasons(load_state()["games"], index)})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/emulators/recommend":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            platform = parse_qs(parsed.query).get("platform", [""])[0]
            self.send_json(200, {"recommendations": recommendations_for_platform(platform)})
            return
        if parsed.path == "/api/emulators/dependencies":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            name = parse_qs(parsed.query).get("name", [""])[0]
            self.send_json(200, detect_dependencies(name))
            return
        if parsed.path == "/api/media/duplicates":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"groups": find_duplicate_media(load_state()["games"])})
            return
        if parsed.path == "/api/media/queue":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"queue": load_media_queue(DATA.parent / "media-queue.json")})
            return
        if parsed.path == "/api/saves/scan":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            found = scan_all_saves(load_state()["games"])
            self.send_json(200, {"games": {str(key): value for key, value in found.items()}, "count": len(found)})
            return
        if parsed.path == "/api/highscores":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                game = load_state()["games"][int(parse_qs(parsed.query)["id"][0])]
                self.send_json(200, {"scores": read_local_highscores(game)})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/obs/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, obs_recording_status())
            return
        if parsed.path == "/api/platform/documents":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            platform = parse_qs(parsed.query).get("platform", [""])[0]
            docs = load_state().get("settings", {}).get("platform_documents", {})
            self.send_json(200, {"documents": docs.get(platform, []) if platform else docs})
            return
        if parsed.path == "/api/platform/document":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            try:
                platform = query["platform"][0]
                index = int(query["index"][0])
                document = load_state().get("settings", {}).get("platform_documents", {}).get(platform, [])[index]
                path = Path(document["path"])
                if not path.is_file():
                    raise FileNotFoundError
                self.send_response(200)
                self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                safe_name = re.sub(r'[\r\n"]', "_", path.name)
                self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)
            except (KeyError, IndexError, ValueError, FileNotFoundError):
                self.send_json(404, {"error": "Platform document not found"})
            return
        if parsed.path == "/api/storefront/catalog":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            source = parse_qs(parsed.query).get("source", [""])[0]
            try:
                self.send_json(200, {"catalog": storefront_catalog(source, settings=load_state().get("settings", {}))})
            except (ValueError, OSError, FileNotFoundError, subprocess.SubprocessError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if parsed.path == "/api/gameyfin/install/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            gameyfin_id = str(query.get("gameyfin_id", [""])[0]).strip()
            if not gameyfin_id:
                self.send_json(400, {"error": "gameyfin_id is required."})
                return
            with PROCESS_LOCK:
                job = dict(INSTALLS.get(f"gameyfin:{gameyfin_id}", {"state": "idle"}))
            self.send_json(200, job)
            return
        if parsed.path == "/api/gameyfin/providers":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                _catalog, providers = catalog_gameyfin(load_state().get("settings", {}))
                self.send_json(200, {"providers": providers})
            except (ValueError, OSError, TypeError, AttributeError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if parsed.path == "/api/save-tools/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, save_tool_status())
            return
        if parsed.path == "/api/plugins/catalog":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"catalog": fetch_plugin_catalog()})
            return
        if parsed.path == "/api/premium/strings":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            locale = parse_qs(parsed.query).get("locale", ["en"])[0]
            self.send_json(200, {"locale": locale, "strings": strings_for(locale)})
            return
        if parsed.path == "/api/premium/media-packs":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"packs": list_media_packs(load_state().get("settings", {}))})
            return
        if parsed.path == "/api/premium/platform-categories":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"categories": platform_categories(load_state().get("settings", {}))})
            return
        if parsed.path == "/api/filter-presets":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            state = load_state()
            self.send_json(200, {
                "presets": list_presets(state),
                "bigbox_quick": bigbox_quick_presets(state),
            })
            return
        if parsed.path == "/api/explorer/facets":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            field = parse_qs(parsed.query).get("field", ["genre"])[0]
            state = load_state()
            self.send_json(200, {"field": field, "facets": explorer_facets(state["games"], field)})
            return
        if parsed.path == "/api/launcher/menu":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            payload = public_state()
            self.send_json(200, {"items": launcher_menu_items(payload["games"])})
            return
        if parsed.path == "/api/import/exclusions":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"exclusions": list_exclusions(load_state())})
            return
        if parsed.path == "/api/emulators/definitions":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"definitions": load_definitions(ROOT / "emulator_defs")})
            return
        if parsed.path == "/api/emulators/scan-configs":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"configs": list_scan_configs(load_state())})
            return
        if parsed.path == "/api/metadata/igdb/search":
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
        if parsed.path == "/api/backup/manifest":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"items": sorted(BACKUP_ITEMS)})
            return
        self.send_json(404, {"error": "Not found"})

    def _do_POST(self):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            payload = self.body()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            route = urlparse(self.path).path
            if route == "/api/launch":
                self.launch(payload)
            elif route == "/api/session/control":
                self.control_session(payload)
            elif route == "/api/favorite":
                self.favorite(payload)
            elif route == "/api/game":
                self.save_game(payload)
            elif route == "/api/game/delete":
                self.delete_game(payload)
            elif route == "/api/games/delete-steam":
                self.delete_steam_games(payload)
            elif route == "/api/games/bulk":
                self.bulk_edit(payload)
            elif route == "/api/games/bulk-wizard":
                self.bulk_wizard(payload)
            elif route == "/api/premium/media-packs/apply":
                self.apply_media_pack_route(payload)
            elif route == "/api/metadata/trailer":
                self.download_trailer(payload)
            elif route == "/api/metadata/gog":
                self.download_gog_route(payload)
            elif route == "/api/bigbox/mode":
                self.bigbox_mode_switch(payload)
            elif route == "/api/import":
                self.import_folder(payload)
            elif route == "/api/import/wizard":
                self.import_wizard(payload)
            elif route == "/api/import/xbox360":
                self.import_xbox360(payload)
            elif route == "/api/import/loose-arcade":
                self.import_loose_arcade_route(payload)
            elif route == "/api/import/watch":
                self.scan_watch_folders()
            elif route == "/api/import/steam":
                self.import_steam_games()
            elif route == "/api/import/heroic":
                self.import_heroic_games()
            elif route == "/api/import/lutris":
                self.import_lutris_games()
            elif route == "/api/import/arcade":
                self.import_arcade_games(payload)
            elif route == "/api/metadata/steam":
                self.steam_metadata(payload)
            elif route == "/api/metadata/sync":
                self.sync_metadata()
            elif route == "/api/metadata/apply":
                self.apply_metadata(payload)
            elif route == "/api/media/bulk":
                self.bulk_media(payload)
            elif route == "/api/profiles":
                self.save_profiles(payload)
            elif route == "/api/settings":
                self.save_settings(payload)
            elif route == "/api/image-group":
                self.save_image_group(payload)
            elif route == "/api/cloud/sync":
                self.send_json(200, sync_cloud())
            elif route == "/api/update/install":
                update = check_update()
                self.send_json(200, install_update(update))
            elif route == "/api/desktop/install":
                self.send_json(200, {"desktop": install_desktop_entry()})
            elif route == "/api/emulators/install":
                self.install_emulator(payload)
            elif route == "/api/emulators/install-all":
                self.install_all_emulators()
            elif route == "/api/emulators/update":
                self.update_one_emulator(payload)
            elif route == "/api/emulators/update-all":
                self.update_all_emulators_route()
            elif route == "/api/emulators/open":
                self.open_emulator(payload)
            elif route == "/api/import/scummvm":
                self.import_scummvm_games()
            elif route == "/api/import/rpcs3":
                self.import_rpcs3_games()
            elif route == "/api/import/vita3k":
                self.import_vita3k_games()
            elif route == "/api/ra/inject":
                self.inject_ra()
            elif route == "/api/bezels/download":
                self.download_bezels(payload)
            elif route == "/api/emumovies/settings":
                self.save_emumovies(payload)
            elif route == "/api/emumovies/download":
                self.emumovies_download(payload)
            elif route == "/api/media/cleanup":
                self.cleanup_media(payload)
            elif route == "/api/screenshot":
                self.take_screenshot(payload)
            elif route == "/api/obs/attach":
                self.obs_attach(payload)
            elif route == "/api/saves/scan/apply":
                self.apply_save_scan(payload)
            elif route == "/api/platform/documents":
                self.save_platform_documents(payload)
            elif route == "/api/storefront/import":
                self.import_storefront_catalog(payload)
            elif route == "/api/gameyfin/test":
                self.test_gameyfin(payload)
            elif route == "/api/gameyfin/install":
                self.install_gameyfin(payload)
            elif route == "/api/gameyfin/uninstall":
                self.uninstall_gameyfin(payload)
            elif route == "/api/save-tools/ludusavi":
                self.run_ludusavi_tool(payload)
            elif route == "/api/save-tools/hoard":
                self.run_hoard_tool(payload)
            elif route == "/api/highscores/export":
                self.export_game_highscores(payload)
            elif route == "/api/highscores/import":
                self.import_game_highscores(payload)
            elif route == "/api/plugins/catalog/install":
                self.install_catalog_plugin(payload)
            elif route == "/api/themes/open-folder":
                self.open_themes_folder()
            elif route == "/api/shutdown":
                self.shutdown(payload)
            elif route == "/api/ra/settings":
                self.save_ra_settings(payload)
            elif route == "/api/ra/game":
                self.ra_game(payload)
            elif route == "/api/plugins/install":
                self.install_plugin(payload)
            elif route == "/api/plugins/toggle":
                self.toggle_plugin(payload)
            elif route == "/api/plugins/remove":
                self.remove_plugin(payload)
            elif route == "/api/extra/launch":
                self.launch_extra(payload)
            elif route == "/api/saves/backup":
                self.backup_game_saves(payload)
            elif route == "/api/saves/restore":
                self.restore_game_saves(payload)
            elif route == "/api/saves/add":
                self.add_game_save_path(payload)
            elif route == "/api/themes/select":
                self.select_theme(payload)
            elif route == "/api/themes/import":
                self.import_theme(payload)
            elif route == "/api/playlists":
                self.save_playlist(payload)
            elif route == "/api/playlists/delete":
                self.delete_playlist(payload)
            elif route == "/api/filter-presets":
                self.save_filter_preset(payload)
            elif route == "/api/filter-presets/delete":
                self.delete_filter_preset(payload)
            elif route == "/api/import/exclusions":
                self.add_import_exclusion(payload)
            elif route == "/api/import/exclusions/delete":
                self.remove_import_exclusion(payload)
            elif route == "/api/backup/create":
                self.create_library_backup(payload)
            elif route == "/api/backup/restore":
                self.restore_library_backup(payload)
            elif route == "/api/emulators/scan":
                self.scan_emulator_folder_route(payload)
            elif route == "/api/emulators/scan-configs":
                self.save_emulator_scan_config(payload)
            elif route == "/api/metadata/igdb/apply":
                self.apply_igdb_metadata(payload)
            elif route == "/api/health":
                self.health()
            elif route == "/api/health/dedupe":
                self.dedupe()
            else:
                self.send_json(404, {"error": "Not found"})
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, json.JSONDecodeError, GameyfinError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
            LOGGER.warning("Request %s failed: %s", urlparse(self.path).path, error)
            self.send_json(400, {"error": str(error)})

    def _handle_request(self, method):
        path = urlparse(self.path).path
        LOGGER.debug("HTTP %s %s started", method, path)
        try:
            getattr(self, f"_{method}")()
        except Exception:
            LOGGER.exception("Unhandled HTTP %s %s", method, path)
            self.send_json(500, {"error": "Unexpected server error. Copy the diagnostic log from Settings and include it in your report."})

    def do_GET(self):
        self._handle_request("do_GET")

    def do_POST(self):
        self._handle_request("do_POST")

    def launch(self, payload):
        self.send_json(200, {"ok": True, **start_game(int(payload["id"]))})

    def control_session(self, payload):
        launch_id = str(payload.get("launch_id", ""))
        action = str(payload.get("action", ""))
        self.send_json(200, control_game_session(launch_id, action))

    def favorite(self, payload):
        with STATE_LOCK:
            state = load_state()
            game = state["games"][int(payload["id"])]
            game["favorite"] = not game.get("favorite", False)
            save_state(state)
        self.send_json(200, {"favorite": game["favorite"]})

    def save_game(self, payload):
        source = payload.get("game", {})
        game = {key: str(source[key]).strip() for key in FIELDS if key in source}
        game["extract_archive"] = bool(source.get("extract_archive"))
        game["hidden"] = bool(source.get("hidden"))
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
        with STATE_LOCK:
            state = load_state()
            if payload.get("id") is None:
                game["added_at"] = datetime.now().isoformat(timespec="seconds")
                state["games"].append(game)
            else:
                existing = state["games"][int(payload["id"])]
                existing.update(game)
            save_state(state)
        self.send_json(200, {"ok": True})

    def bulk_edit(self, payload):
        with STATE_LOCK:
            state = load_state()
            changed = bulk_update(state["games"], payload.get("ids"), payload.get("changes"))
            save_state(state)
        self.send_json(200, {"updated": changed})

    def delete_game(self, payload):
        delete_media = bool(payload.get("delete_media"))
        with STATE_LOCK:
            state = load_state()
            game = state["games"].pop(int(payload["id"]))
            if delete_media:
                for path in game_media_paths(game):
                    try:
                        target = Path(path).expanduser()
                        if target.is_file():
                            target.unlink()
                    except OSError:
                        pass
            save_state(state)
        self.send_json(200, {"removed": game.get("name", "")})

    def delete_steam_games(self, payload):
        with STATE_LOCK:
            state = load_state()
            games = state["games"]
            state["games"] = [game for game in games if str(game.get("source", "")).casefold() != "steam"]
            removed = len(games) - len(state["games"])
            save_state(state)
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
        self.send_json(200, {"added": added, "found": found, "recommendations": recommendations})

    def import_wizard(self, payload):
        folder = str(payload.get("folder", "")).strip()
        chosen = payload.get("chosen_emulators", {})
        if not isinstance(chosen, dict):
            raise ValueError("chosen_emulators must be an object.")
        added, found, recommendations = import_folder_path(folder, chosen_emulators=chosen)
        installs = []
        for platform, app_id in chosen.items():
            if not app_id:
                continue
            try:
                install_emulator(str(app_id))
                installs.append(str(app_id))
            except (OSError, ValueError):
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
        with STATE_LOCK:
            state = load_state()
            existing = {str(game.get("steam_app_id")) for game in state["games"] if game.get("steam_app_id")}
            new_games = [game for game in imported if game["steam_app_id"] not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        self.send_json(200, {"added": len(new_games), "found": len(imported)})

    def import_heroic_games(self):
        imported = import_heroic()
        with STATE_LOCK:
            state = load_state()
            existing = {
                (game.get("source"), str(game.get("heroic_app_id")))
                for game in state["games"] if game.get("heroic_app_id")
            }
            new_games = [game for game in imported if (game["source"], game["heroic_app_id"]) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        self.send_json(200, {"added": len(new_games), "found": len(imported)})

    def import_lutris_games(self):
        imported = import_lutris()
        with STATE_LOCK:
            state = load_state()
            existing = {str(game.get("lutris_id")) for game in state["games"] if game.get("lutris_id")}
            new_games = [game for game in imported if game["lutris_id"] not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        self.send_json(200, {"added": len(new_games), "found": len(imported)})

    def import_arcade_games(self, payload):
        imported = import_arcade(
            str(payload.get("folder", "")),
            str(payload.get("dat", "")),
            str(payload.get("command", "")),
            str(payload.get("source", "MAME")),
        )
        with STATE_LOCK:
            state = load_state()
            existing = {
                (game.get("source"), str(game.get("rom_name")))
                for game in state["games"] if game.get("rom_name")
            }
            new_games = [game for game in imported if (game["source"], game["rom_name"]) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        counts = {kind: sum(game["set_type"] == kind for game in imported) for kind in ("parent", "merged", "split", "non-merged")}
        self.send_json(200, {"added": len(new_games), "found": len(imported), "sets": counts})

    def steam_metadata(self, payload):
        with STATE_LOCK:
            state = load_state()
            game = state["games"][int(payload["id"])]
            update_steam_metadata(game)
            save_state(state)
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

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state":"downloading"})

    def apply_metadata(self, payload):
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        index = int(payload["id"])
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not set(media_types) <= {"cover", "background", "screenshots"}:
            raise ValueError("Invalid media selection.")
        state = load_state()
        original = dict(state["games"][index])
        updated = apply_game_metadata(
            dict(original), METADATA_DATABASE, int(payload["database_id"]), media_types,
            DATA.parent / "media/launchbox", bool(payload.get("overwrite")),
            region_priority=load_state().get("settings", {}).get("region_priority"),
        )
        changes = {key:value for key,value in updated.items() if original.get(key) != value}
        with STATE_LOCK:
            state = load_state()
            state["games"][index].update(changes)
            save_state(state)
        self.send_json(200, {"updated":sorted(changes)})

    def bulk_media(self, payload):
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not media_types or not set(media_types) <= {"cover", "background", "screenshots"}:
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
                index for index, game in enumerate(state["games"])
                if game.get("launchbox_db_id") and (platform == "all" or game.get("platform") == platform)
            ]
            with PROCESS_LOCK:
                MEDIA_JOB["total"] = len(targets)
            updated_count, errors = 0, []
            for current, index in enumerate(targets, 1):
                original = {}
                try:
                    state = load_state()
                    original = dict(state["games"][index])
                    updated = apply_game_metadata(
                        dict(original), METADATA_DATABASE, int(original["launchbox_db_id"]), media_types,
                        DATA.parent / "media/launchbox", overwrite,
                    )
                    changes = {key:value for key,value in updated.items() if original.get(key) != value}
                    if changes:
                        with STATE_LOCK:
                            state = load_state()
                            state["games"][index].update(changes)
                            save_state(state)
                        updated_count += 1
                except (OSError, ValueError, sqlite3.Error) as error:
                    errors.append(f"{original.get('name', index)}: {error}")
                with PROCESS_LOCK:
                    MEDIA_JOB.update({"current":current, "updated":updated_count, "errors":errors[-20:]})
            with PROCESS_LOCK:
                MEDIA_JOB["state"] = "done"

        threading.Thread(target=worker, daemon=True).start()
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
        with STATE_LOCK:
            state = load_state()
            state["profiles"] = clean
            save_state(state)
        self.send_json(200, {"saved": len(clean)})

    def save_settings(self, payload):
        with STATE_LOCK:
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
        if image_group not in {"cover", "background", "screenshot"}:
            raise ValueError("Unknown default image group.")
        save_backup_limit = int(merged.get("save_backup_limit", 10))
        if not 0 <= save_backup_limit <= 500:
            raise ValueError("Save backup limit must be between 0 and 500.")
        media_download_limit = int(merged.get("media_download_limit", 0))
        if media_download_limit < 0 or media_download_limit > 10000:
            raise ValueError("Media download limit must be between 0 and 10000.")
        auto_import_media_types = merged.get("auto_import_media_types", [])
        if not isinstance(auto_import_media_types, list) or not set(auto_import_media_types) <= {"cover", "background", "screenshots"}:
            raise ValueError("Auto-import media types must be cover, background, and/or screenshots.")
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
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            gameyfin_password = str(merged.get("gameyfin_password", "")).strip()
            settings.update({
                "watch_folders": clean_folders,
                "screensaver_seconds": seconds,
                "controller_map": clean_mapping,
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
                "progress_on_first_play": progress_on_first_play,
                "auto_close_store_clients": bool(merged.get("auto_close_store_clients", False)),
            })
            save_state(state)
        self.send_json(200, public_settings(state))

    def save_image_group(self, payload):
        group = str(payload.get("group", ""))
        scope = str(payload.get("scope", "global"))
        name = str(payload.get("name", "")).strip()
        if group not in {"default", "cover", "background", "screenshot"} or scope not in {"global", "platform", "playlist"}:
            raise ValueError("Unknown image group.")
        if scope != "global" and (not name or len(name) > 200):
            raise ValueError("A platform or playlist is required.")
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            if scope == "global":
                settings["image_group"] = "cover" if group == "default" else group
            else:
                mappings = settings.setdefault(f"image_group_by_{scope}", {})
                if group == "default":
                    mappings.pop(name, None)
                else:
                    mappings[name] = group
            save_state(state)
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
                with STATE_LOCK:
                    state = load_state()
                    for platform, command in profiles.items():
                        state["profiles"].setdefault(platform, command)
                    save_state(state)
                job = {"state": "done", "profiles": profiles}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[app_id] = job

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state": "installing"})

    def install_all_emulators(self):
        def worker():
            result = install_all_emulators()
            with PROCESS_LOCK:
                INSTALLS["__all__"] = {"state": "done", **result}

        with PROCESS_LOCK:
            if INSTALLS.get("__all__", {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS["__all__"] = {"state": "installing"}
        threading.Thread(target=worker, daemon=True).start()
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
        index = int(payload["id"])
        state = load_state()
        game = state["games"][index]
        game_id, digest = match_ra_game(game, credentials, DATA.parent / "cache/retroachievements")
        with STATE_LOCK:
            state = load_state()
            state["games"][index]["ra_game_id"] = str(game_id)
            state["games"][index]["ra_hash"] = digest
            save_state(state)
        progress = ra_game_progress(game_id, credentials)
        progress["game_id"] = game_id
        progress = enhanced_ra_profile(progress, credentials)
        self.send_json(200, progress)

    def bulk_wizard(self, payload):
        changes = bulk_wizard_changes(payload.get("changes", {}))
        with STATE_LOCK:
            state = load_state()
            changed = bulk_update(state["games"], payload.get("ids"), changes)
            save_state(state)
        self.send_json(200, {"updated": changed, "fields": list(changes.keys())})

    def apply_media_pack_route(self, payload):
        pack_id = str(payload.get("id", "")).strip()
        with STATE_LOCK:
            state = load_state()
            pack = apply_media_pack(state, pack_id)
            save_state(state)
        self.send_json(200, {"pack": pack, "settings": public_settings(state)})

    def download_trailer(self, payload):
        index = int(payload["id"])
        with STATE_LOCK:
            state = load_state()
            game = state["games"][index]
            path = download_steam_trailer(game, DATA.parent / "media")
            save_state(state)
        self.send_json(200, {"video_trailer": path})

    def download_gog_route(self, payload):
        index = int(payload["id"])
        with STATE_LOCK:
            state = load_state()
            game = state["games"][index]
            download_gog_media(game, DATA.parent / "media")
            save_state(state)
        self.send_json(200, {"cover": game.get("cover", ""), "background": game.get("background", "")})

    def bigbox_mode_switch(self, payload):
        entering = bool(payload.get("entering"))
        key = "bigbox_shutdown_commands" if entering else "shutdown_commands"
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
        self.send_json(200, {"plugin":manifest})

    def toggle_plugin(self, payload):
        enabled = set_plugin_enabled(
            DATA.parent / "plugins",
            str(payload.get("id", "")),
            bool(payload.get("enabled")),
        )
        self.send_json(200, {"enabled":enabled})

    def remove_plugin(self, payload):
        plugin_id = remove_plugin(DATA.parent / "plugins", str(payload.get("id", "")))
        self.send_json(200, {"removed":plugin_id})

    def launch_extra(self, payload):
        state = load_state()
        game = state["games"][int(payload["id"])]
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
        game = load_state()["games"][int(payload["id"])]
        archive = backup_saves(game, DATA.parent / "save-backups")
        removed = enforce_backup_limit(game, DATA.parent / "save-backups", load_state().get("settings", {}).get("save_backup_limit", 10))
        self.send_json(200, {"backup": archive.name, "trimmed": removed})

    def restore_game_saves(self, payload):
        game = load_state()["games"][int(payload["id"])]
        archive = restore_saves(game, DATA.parent / "save-backups", str(payload["backup"]))
        self.send_json(200, {"restored": archive.name})

    def add_game_save_path(self, payload):
        path = Path(str(payload.get("path", ""))).expanduser()
        if not path.exists():
            raise FileNotFoundError("Save path does not exist.")
        with STATE_LOCK:
            state = load_state()
            paths = state["games"][int(payload["id"])].setdefault("save_paths", [])
            if str(path) not in paths:
                paths.append(str(path))
            save_state(state)
        self.send_json(200, {"path":str(path)})

    def select_theme(self, payload):
        name = str(payload.get("name", "")).strip()
        platform = str(payload.get("platform", "")).strip()
        if name and not (DATA.parent / "themes" / f"{Path(name).stem}.css").is_file():
            raise FileNotFoundError("Theme not found.")
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            if platform:
                mappings = settings.setdefault("theme_by_platform", {})
                if name:
                    mappings[platform] = name
                else:
                    mappings.pop(platform, None)
            else:
                settings["theme"] = name
            save_state(state)
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
        clean = {
            key: str(rules.get(key, "")).strip()
            for key in ("platform", "view", "query")
            if str(rules.get(key, "")).strip()
        }
        with STATE_LOCK:
            state = load_state()
            playlists = state.setdefault("playlists", [])
            existing = next((item for item in playlists if item.get("name") == name), None)
            if existing:
                existing["rules"] = clean
            else:
                playlists.append({"name": name, "rules": clean})
            save_state(state)
        self.send_json(200, {"saved": name})

    def delete_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        with STATE_LOCK:
            state = load_state()
            state["playlists"] = [item for item in state.get("playlists", []) if item.get("name") != name]
            save_state(state)
        self.send_json(200, {"deleted": name})

    def save_filter_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        rules = payload.get("rules", {})
        bigbox_quick = bool(payload.get("bigbox_quick", False))
        with STATE_LOCK:
            state = load_state()
            save_preset(state, name, rules, bigbox_quick=bigbox_quick)
            save_state(state)
        self.send_json(200, {"saved": name})

    def delete_filter_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        with STATE_LOCK:
            state = load_state()
            if not delete_preset(state, name):
                raise ValueError("Preset not found.")
            save_state(state)
        self.send_json(200, {"deleted": name})

    def add_import_exclusion(self, payload):
        source = str(payload.get("source", "")).strip()
        external_id = str(payload.get("external_id", "")).strip()
        heroic_source = str(payload.get("heroic_source", "")).strip()
        with STATE_LOCK:
            state = load_state()
            entry = add_exclusion(state, source, external_id, heroic_source=heroic_source)
            save_state(state)
        self.send_json(200, {"exclusion": entry})

    def remove_import_exclusion(self, payload):
        source = str(payload.get("source", "")).strip()
        external_id = str(payload.get("external_id", "")).strip()
        with STATE_LOCK:
            state = load_state()
            remove_exclusion(state, source, external_id)
            save_state(state)
        self.send_json(200, {"removed": True})

    def create_library_backup(self, payload):
        items = payload.get("items", ["library", "settings"])
        keep = int(payload.get("keep", 0))
        with STATE_LOCK:
            state = load_state()
            archive = create_backup(DATA.parent, state, items, keep=keep, running_map=RUNNING)
        self.send_json(200, {"archive": str(archive), "name": archive.name})

    def restore_library_backup(self, payload):
        archive = Path(str(payload.get("path", ""))).expanduser()
        items = payload.get("items")
        if not archive.is_file():
            raise FileNotFoundError("Backup archive not found.")
        with STATE_LOCK:
            restored = restore_backup(archive, DATA.parent, items=items, running_map=RUNNING)
        self.send_json(200, {"restored": restored})

    def scan_emulator_folder_route(self, payload):
        folder = str(payload.get("folder", "")).strip()
        imported = scan_emulator_folder(folder)
        added, found = merge_imported_games(imported, lambda game: ("path", str(game.get("path", ""))))
        self.send_json(200, {"added": added, "found": found})

    def save_emulator_scan_config(self, payload):
        folder = str(payload.get("folder", "")).strip()
        emulator_id = str(payload.get("emulator_id", "")).strip()
        auto_update = bool(payload.get("auto_update", False))
        with STATE_LOCK:
            state = load_state()
            entry = save_scan_config(state, folder, emulator_id, auto_update=auto_update)
            save_state(state)
        self.send_json(200, {"config": entry})

    def apply_igdb_metadata(self, payload):
        game_id = int(payload["id"])
        igdb_id = int(payload["igdb_id"])
        metadata = fetch_igdb_game(igdb_id)
        with STATE_LOCK:
            state = load_state()
            game = state["games"][game_id]
            apply_igdb_metadata(game, metadata)
            save_state(state)
        self.send_json(200, {"applied": True, "game": game.get("name", "")})

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
        with STATE_LOCK:
            state = load_state()
            seen, kept, removed = set(), [], []
            for game in state["games"]:
                identity = game_identity(game)
                if identity in seen:
                    removed.append(game.get("name", ""))
                else:
                    seen.add(identity)
                    kept.append(game)
            state["games"] = kept
            save_state(state)
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

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state": "updating"})

    def update_all_emulators_route(self):
        with PROCESS_LOCK:
            if INSTALLS.get("__update_all__", {}).get("state") == "updating":
                self.send_json(200, {"state": "updating"})
                return
            INSTALLS["__update_all__"] = {"state": "updating"}

        def worker():
            result = update_all_emulators()
            with PROCESS_LOCK:
                INSTALLS["__update_all__"] = {"state": "done", **result}

        threading.Thread(target=worker, daemon=True).start()
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
        index = int(payload["id"])
        with STATE_LOCK:
            state = load_state()
            game = state["games"][index]
            path = download_emumovies_media(
                game, credentials, DATA.parent / "media", str(payload.get("type", "box")),
            )
            game["cover"] = path
            save_state(state)
        self.send_json(200, {"path": path})

    def cleanup_media(self, payload):
        groups = find_duplicate_media(load_state()["games"])
        apply = bool(payload.get("apply"))
        deleted = cleanup_duplicates(groups, dry_run=not apply)
        self.send_json(200, {"groups": len(groups), "paths": deleted, "applied": apply})

    def take_screenshot(self, payload):
        index = int(payload["id"])
        state = load_state()
        game = state["games"][index]
        destination = DATA.parent / "media" / "captures" / f"{Path(game.get('path', 'game')).stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        path = capture_screenshot(destination)
        with STATE_LOCK:
            state = load_state()
            screenshots = state["games"][index].setdefault("screenshots", [])
            if path not in screenshots:
                screenshots.append(path)
            save_state(state)
        self.send_json(200, {"path": path})

    def obs_attach(self, payload):
        index = int(payload["id"])
        video_path = str(payload.get("path", "")).strip()
        with STATE_LOCK:
            state = load_state()
            game = state["games"][index]
            path = attach_recording(game, video_path)
            save_state(state)
        self.send_json(200, {"path": path, "obs": obs_recording_status()})

    def apply_save_scan(self, payload):
        state = load_state()
        found = scan_all_saves(state["games"])
        updated = 0
        with STATE_LOCK:
            state = load_state()
            for index, paths in found.items():
                save_paths = state["games"][index].setdefault("save_paths", [])
                for path in paths:
                    if path not in save_paths:
                        save_paths.append(path)
                        updated += 1
            save_state(state)
        self.send_json(200, {"updated": updated, "games": len(found)})

    def save_platform_documents(self, payload):
        platform = str(payload.get("platform", "")).strip()
        if not platform:
            raise ValueError("Platform is required.")
        documents = self.clean_extras(payload.get("documents", []), command=False)
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            settings.setdefault("platform_documents", {})[platform] = documents
            save_state(state)
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
            identity = lambda game: ("steam", str(game.get("steam_app_id", "")))
        elif source.casefold() == "heroic":
            identity = lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", "")))
        elif source.casefold() == "lutris":
            identity = lambda game: ("lutris", str(game.get("lutris_id", "")))
        elif source.casefold() == "gameyfin":
            identity = lambda game: ("gameyfin", str(game.get("gameyfin_id", "")))
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
                with STATE_LOCK:
                    settings = dict(load_state().get("settings", {}))
                installed = install_gameyfin_game(settings, game_id)
                with STATE_LOCK:
                    state = load_state()
                    target = None
                    for game in state["games"]:
                        if str(game.get("gameyfin_id") or "") == game_id:
                            target = game
                            break
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
                    save_state(state)
                result = {"state": "done", "gameyfin_id": game_id, "game": installed}
            except (GameyfinError, OSError, ValueError, IndexError, KeyError) as error:
                result = {"state": "error", "gameyfin_id": game_id, "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[job_key] = result

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state": "installing", "gameyfin_id": game_id})

    def uninstall_gameyfin(self, payload):
        index = int(payload["id"])
        with STATE_LOCK:
            state = load_state()
            game = state["games"][index]
            if not game.get("gameyfin_id"):
                raise ValueError("This game is not a Gameyfin entry.")
            result = uninstall_gameyfin_game(game)
            save_state(state)
        self.send_json(200, result)

    def run_ludusavi_tool(self, payload):
        settings = load_state().get("settings", {})
        game_name = str(payload.get("name", ""))
        if "id" in payload and not game_name:
            game_name = load_state()["games"][int(payload["id"])].get("name", "")
        result = run_ludusavi(
            str(payload.get("action", "backup")),
            game_name=game_name,
            path=str(payload.get("path") or settings.get("ludusavi_backup_path", "")),
        )
        self.send_json(200, result)

    def run_hoard_tool(self, payload):
        game_name = str(payload.get("name", ""))
        if "id" in payload and not game_name:
            game_name = load_state()["games"][int(payload["id"])].get("name", "")
        result = run_hoard(str(payload.get("action", "backup")), game_name=game_name)
        self.send_json(200, result)

    def export_game_highscores(self, payload):
        index = int(payload["id"])
        state = load_state()
        game = state["games"][index]
        export_dir = DATA.parent / "highscores" / re.sub(r"[^a-z0-9]+", "-", str(game.get("name", "game")).casefold()).strip("-")
        result = export_highscores(game, export_dir)
        self.send_json(200, result)

    def import_game_highscores(self, payload):
        index = int(payload["id"])
        import_dir = str(payload.get("path", "")).strip()
        state = load_state()
        game = state["games"][index]
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
        archive = Path(args[args.index("--restore-backup") + 1]).expanduser()
        restored = restore_backup(archive, DATA.parent, running_map=RUNNING)
        print(",".join(restored))
        return
    cli_code = handle_cli(args, DATA.parent)
    if cli_code is not None:
        raise SystemExit(cli_code)
    ensure_stock_themes(DATA.parent / "themes", ROOT)
    with STATE_LOCK:
        state = load_state()
        if purge_demo_games(state):
            save_state(state)
        profiles = state.setdefault("profiles", {})
        profiles.update(merge_profiles_from_definitions(profiles))
        save_state(state)
    WATCH_STOP.clear()
    threading.Thread(target=auto_import_worker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    run_configured_commands("startup_commands")
    port = server.server_address[1]
    (DATA.parent / "server.port").write_text(str(port))
    (DATA.parent / "server.token").write_text(TOKEN)
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    force_game_mode = "--game-mode" in sys.argv
    guest = is_gamescope_guest(force=force_game_mode)
    print(url, flush=True)
    if "--no-browser" not in sys.argv:
        opened = open_ui(url, guest=guest, force_game_mode=force_game_mode)
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WATCH_STOP.set()
        server.server_close()
        run_configured_commands("shutdown_commands")


if __name__ == "__main__":
    main()
