"""Process-global state and shared service functions for the OpenBox web server.

Owns the mutable module state (TOKEN, locks, running sessions, caches) and the
service helpers that ``web_app.Handler`` mixin methods and ``web_app.main()``
call as bare names. ``web_app.py`` and ``handlers/*.py`` import them from here
so every reference resolves statically.
"""

import copy
from datetime import datetime
import html
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

from automation import MAX_WEBHOOKS, build_event, utc_now
from backend_io import contained_path, download_file, read_limited
from catalog import apply_progress_automation
from cloud_sync import sync_statistics
from importers import import_heroic, import_lutris, import_steam
from job_manager import JobManager
from notifications import add_notification
import openbox
from openbox import DATA, EXTENSIONS, PLATFORM_BY_EXTENSION, STATE_STORE, build_launch, load_state, load_state_readonly, update_state, update_state_with_result
from parity_discovery import clear_discovery_cache, discovery_lists
from parity_emulator_defs import list_scan_configs, scan_folder as scan_emulator_folder
from parity_filter_presets import list_presets
from parity_gamescope import is_gamescope_guest, is_steam_launch, mark_process_windows, steam_game_id_for
from parity_gameyfin import GameyfinError, catalog_gameyfin
from parity_identity import cross_source_identity, source_family, source_identities
from parity_import import detect_dependencies, import_multi_platform, recommend_emulators
from parity_import_policy import filter_imported, list_exclusions
from parity_integrations import auto_attach_obs_recording, load_emumovies_credentials
from parity_media import REGION_PRIORITY_DEFAULT, active_video, enqueue_media_job, media_types_from_settings, normalize_video_fields
from parity_perf import apply_perf_profile, effective_profile_name, restore_perf_profile
from parity_premium import LIST_COLUMNS_DEFAULT, category_for_platform, custom_field_defs, import_with_emulator_choice, list_media_packs, platform_categories, strings_for
from parity_saves import enforce_backup_limit, games_with_saves
from parity_save_tools import save_tool_status
from parity_storefront import catalog_entries_to_games, storefront_catalog
from parity_tracking import close_store_client, wait_for_exit
from plugins import run_plugins
from retroachievements import load_credentials as load_ra_credentials
from saves import backup_saves, discover_save_paths, list_backups, restore_saves
from updates import VERSION

from pkg.state.cache import (
    FILE_PROBE_CACHE,
    FILE_PROBE_LOCK,
    FILE_PROBE_MAX,
    FILE_PROBE_TTL,
    MEDIA_EPOCH,
    MEDIA_EPOCH_LOCK,
    PLUGIN_EPOCH,
    PLUGIN_LIBRARY_CACHE,
    PLUGIN_LIBRARY_LOCK,
    PLUGIN_LIBRARY_TTL,
    PUBLIC_SETTINGS_CACHE,
    PUBLIC_SETTINGS_LOCK,
    PUBLIC_STATE_CACHE,
    PUBLIC_STATE_LOCK,
    STATE_LOCK,
    STATE_VIEW_CACHE,
    STATE_VIEW_LOCK,
    _GAME_PROJECTION_CACHE,
    _GAME_PROJECTION_LOCK,
    _GAME_PROJECTION_MAX,
    _KNOWN_MEDIA_MAX,
    _KNOWN_MEDIA_SET_CACHE,
    _KNOWN_MEDIA_SET_LOCK,
    _PLATFORM_CATEGORY_CACHE,
    _PLATFORM_CATEGORY_LOCK,
    _PLATFORM_CATEGORY_MAX,
    _PLUGIN_REFRESH_IN_PROGRESS,
    _SANITIZE_MEDIA_PATH_CACHE,
    _SANITIZE_MEDIA_PATH_LOCK,
    _SANITIZE_MEDIA_PATH_MAX,
    _build_known_media_set,
    _build_public_state,
    _fast_realpath,
    _media_dir_mtime,
    _media_set_contains,
    _project_game,
    _public_settings_uncached,
    _public_state_cached,
    _public_state_signature,
    bump_media_epoch,
    clear_file_probe_cache,
    load_state_view,
    public_settings,
    public_state,
    public_state_bytes,
    public_state_etag,
    transact_state,
)
from pkg.state.launch import (
    PROCESS_LOCK,
    PROCESSES,
    RUNNING,
    SESSION_EVENTS,
    _ReattachedLease,
    _ReattachedProcess,
    _annotate_gamescope_start,
    _apply_start_plugins,
    _contained_launch_cwd,
    _make_start_mutator,
    _match_fallback_index,
    _match_path_and_name,
    _match_storefront_ids,
    _publish_start_events,
    _read_proc_cmdline,
    _read_proc_start_time,
    _resolve_start_game,
    _stable_id_match,
    _start_launch_command,
    _validate_start_command,
    _verify_process_identity,
    control_game_session,
    finish_session,
    game_from_payload,
    game_from_query,
    reattach_session,
    reconcile_sessions_on_startup,
    resolve_library_game,
    start_game,
)
from pkg.state.media_probe import (
    FIELDS,
    MEDIA_PATH_FIELDS,
    MEDIA_ROOTS_ENV,
    MEDIA_TYPES_ALL,
    _media_roots,
    _reject_media_symlink_components,
    approved_backup_file,
    approved_media_path,
    download_image,
    game_media_paths,
    media_probe_path,
    probe_path,
    safe_document_file,
    sanitize_document_records,
    sanitize_media_path,
    update_steam_metadata,
)
from pkg.state.sse import (
    EVENT_SEQUENCE,
    EVENT_SUBSCRIBERS,
    EVENT_SUBSCRIBERS_LOCK,
    GZIP_THRESHOLD,
    METADATA_DATABASE,
    SSE_MAX_EVENT_BYTES,
    SSE_MAX_SUBSCRIBERS,
    SSE_QUEUE_SIZE,
    SSE_WRITE_TIMEOUT,
    WEBHOOK_DISPATCHER,
    WEBHOOK_DISPATCHER_LOCK,
    _close_sse_subscriber,
    _commit_webhook_result,
    _default_webhook_dispatcher_factory,
    _emit_webhook_failure,
    _publish_session_event,
    _webhook_payload,
    broadcast_event,
    emit_notification,
    event_matches,
    get_webhook_dispatcher,
    public_webhook_configs,
    publish_event,
    register_event_subscriber,
    session_event,
    shutdown_webhooks,
    unregister_event_subscriber,
    webhook_configs,
)

ROOT = Path(__file__).parent
TOKEN = secrets.token_urlsafe(24)
LOGGER = logging.getLogger("openbox")

INSTALLS = {}
METADATA_JOB = {}
MEDIA_JOB = {}
JOB_MANAGER = JobManager()
try:
    from pkg.state.sse import broadcast_event

    JOB_MANAGER.set_observer(lambda job: broadcast_event("job.finished", job))
except Exception:
    pass
WATCH_STOP = threading.Event()


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


def _launcher_label(game):
    if game.get("steam_app_id"):
        return "Steam"
    if game.get("heroic_app_id"):
        source = str(game.get("heroic_source") or game.get("source") or "").strip()
        return f"Heroic ({source})" if source else "Heroic"
    if game.get("lutris_id"):
        return "Lutris"
    if game.get("gameyfin_id"):
        return "Gameyfin"
    if game.get("faugus_id"):
        return "Faugus"
    return str(game.get("source") or "Imported").strip() or "Imported"


def _filled_launch_command(game):
    command = str(game.get("launch") or "").strip()
    if not command:
        return ""
    path = str(game.get("path") or "")
    rom_p = Path(path)
    replacements = {
        "{path}": path,
        "{ImagePath}": path,
        "{name}": str(game.get("name", "")),
        "{Name}": str(game.get("name", "")),
        "{dir}": str(rom_p.parent),
        "{Dir}": str(rom_p.parent),
        "{file}": rom_p.name,
        "{File}": rom_p.name,
        "{stem}": rom_p.stem,
        "{FileNameWithoutExtension}": rom_p.stem,
        "{platform}": str(game.get("platform", "")),
        "{Platform}": str(game.get("platform", "")),
        "{app_id}": str(game.get("steam_app_id", "")),
        "{heroic_app_id}": str(game.get("heroic_app_id", "")),
        "{lutris_id}": str(game.get("lutris_id", "")),
        "{rom_name}": str(game.get("rom_name", "")),
        "{DataDir}": str(DATA.parent),
    }
    for marker, value in replacements.items():
        command = command.replace(marker, value)
    return command


def _application_for_game(game):
    path = str(game.get("path") or "").strip()
    if not path:
        return None
    return {
        "name": f"Launch with {_launcher_label(game)}",
        "path": path,
        "command": _filled_launch_command(game),
    }


def _append_unique_application(target, application):
    if not application:
        return False
    applications = target.setdefault("applications", [])
    key = (application.get("path", ""), application.get("command", ""))
    for existing in applications:
        if (existing.get("path", ""), existing.get("command", "")) == key:
            return False
    applications.append(application)
    return True


def _merge_source_fields(target, source):
    source_ids = set(source_identities(target))
    source_ids.update(source_identities(source))
    if source_ids:
        target["source_identities"] = sorted(source_ids)
    if source.get("heroic_app_id") and source.get("source") and not target.get("heroic_source"):
        target["heroic_source"] = str(source.get("source"))
    for field in (
        "steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id", "gameyfin_provider",
        "faugus_id", "install_dir",
    ):
        if source.get(field) and not target.get(field):
            target[field] = source[field]
    for flag in ("owned", "store_catalog", "store_installed"):
        if source.get(flag):
            target[flag] = source[flag]
    alternate_names = target.get("alternate_names")
    if not isinstance(alternate_names, list):
        alternate_names = []
    source_name = str(source.get("name") or "").strip()
    if source_name and source_name != target.get("name") and source_name not in alternate_names:
        alternate_names.append(source_name)
        target["alternate_names"] = alternate_names[:20]


def _merge_imported_game(target, source, *, add_launcher):
    if add_launcher:
        _append_unique_application(target, _application_for_game(source))
    _merge_source_fields(target, source)
    for field in ("cover", "background", "clear_logo", "fanart", "banner", "icon"):
        if source.get(field) and not target.get(field):
            target[field] = source[field]
    if source.get("store_installed") and not target.get("store_installed"):
        for field in ("path", "launch", "platform", "install_dir"):
            if source.get(field):
                target[field] = source[field]
        target["store_installed"] = True


def _index_existing_games(games):
    exact, cross = {}, {}
    for game in games:
        for identity in source_identities(game):
            exact.setdefault(identity, game)
        title_identity = cross_source_identity(game)
        if title_identity:
            cross.setdefault(title_identity, game)
    return exact, cross


def consolidate_existing_games(games):
    exact, cross = {}, {}
    kept, removed = [], []
    for game in games:
        source_keys = source_identities(game)
        target = next((exact[key] for key in source_keys if key in exact), None)
        exact_match = target is not None
        title_identity = cross_source_identity(game)
        if (
            target is None
            and title_identity
            and title_identity in cross
            and source_family(cross[title_identity]) != source_family(game)
        ):
            target = cross.get(title_identity)
        if target is None:
            kept.append(game)
            for key in source_keys:
                exact.setdefault(key, game)
            if title_identity:
                cross.setdefault(title_identity, game)
            continue
        _merge_imported_game(target, game, add_launcher=not exact_match)
        removed.append(game.get("name", ""))
        for key in source_identities(target):
            exact.setdefault(key, target)
        title_identity = cross_source_identity(target)
        if title_identity:
            cross.setdefault(title_identity, target)
    games[:] = kept
    return removed


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
        exact, cross = _index_existing_games(state["games"])
        legacy_existing = {identity_fn(game) for game in state["games"]}
        new_games = []
        timestamp = datetime.now().isoformat(timespec="seconds")
        default_progress = state.get("settings", {}).get("progress_on_first_play", "Playing")
        for game in filtered:
            source_keys = source_identities(game)
            target = next((exact[key] for key in source_keys if key in exact), None)
            if target is not None or identity_fn(game) in legacy_existing:
                if target is not None:
                    _merge_imported_game(target, game, add_launcher=False)
                    for key in source_identities(target):
                        exact.setdefault(key, target)
                continue
            title_identity = cross_source_identity(game)
            if (
                title_identity
                and title_identity in cross
                and source_family(cross[title_identity]) != source_family(game)
            ):
                target = cross[title_identity]
                _merge_imported_game(target, game, add_launcher=True)
                for key in source_identities(target):
                    exact.setdefault(key, target)
                continue
            game["added_at"] = timestamp
            if default_progress and not game.get("progress"):
                game["progress"] = default_progress
            normalize_video_fields(game)
            new_games.append(game)
            legacy_existing.add(identity_fn(game))
            for key in source_identities(game):
                exact.setdefault(key, game)
            if title_identity:
                cross.setdefault(title_identity, game)
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

