"""Import consolidation, folder scanning, and cloud sync logic for OpenBox.

Extracted from webapp_state.py to keep that module a thin re-export shim.
"""

import copy
from datetime import datetime
import json
import logging
import multiprocessing
import queue
import shutil
import threading
from pathlib import Path

from cloud_sync import apply_synced_stats, sync_statistics
from importers import heroic_bases, import_heroic, import_lutris, import_steam, steam_roots
from openbox import DATA, EXTENSIONS, PLATFORM_BY_EXTENSION, load_state, update_state
from plugins import emit_plugin_event
from pkg.parity.parity_emulator_defs import list_scan_configs, scan_folder as scan_emulator_folder
from pkg.parity.launch_tokens import build_launch_args
from pkg.platform_compat import IS_WINDOWS, join_command
from pkg.parity.parity_gameyfin import GameyfinError, catalog_gameyfin
from pkg.parity.parity_identity import cross_source_identity, source_family, source_identities
from pkg.parity.parity_import import import_multi_platform, recommend_emulators
from pkg.parity.parity_import_policy import filter_imported
from pkg.parity.parity_media import (
    MEDIA_QUEUE_BUILTIN_SOURCES,
    enqueue_media_job,
    media_queue_worker,
    media_types_from_settings,
    normalize_video_fields,
)
from pkg.parity.parity_premium import import_with_emulator_choice
from pkg.parity.parity_storefront import catalog_entries_to_games, storefront_catalog

LOGGER = logging.getLogger("openbox")

WATCH_STOP = threading.Event()


def game_identity(game):
    if game.get("steam_app_id"):
        return "steam", str(game["steam_app_id"])
    if game.get("heroic_app_id"):
        return "heroic", str(game.get("source", "")), str(game["heroic_app_id"])
    if game.get("lutris_id"):
        return "lutris", str(game["lutris_id"])
    if game.get("gog_id"):
        return "gog", str(game["gog_id"])
    if game.get("ea_id"):
        return "ea", str(game["ea_id"])
    if game.get("ubisoft_id"):
        return "ubisoft", str(game["ubisoft_id"])
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
    if game.get("gog_id"):
        return "GOG"
    if game.get("ea_id"):
        return "EA app"
    if game.get("ubisoft_id"):
        return "Ubisoft Connect"
    return str(game.get("source") or "Imported").strip() or "Imported"


def _filled_launch_command(game):
    command = str(game.get("launch") or "").strip()
    if not command:
        return ""
    args = build_launch_args(command, game, data_dir=str(DATA.parent))
    return join_command(args)


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
        "faugus_id", "gog_id", "ea_id", "ubisoft_id", "install_dir",
    ):
        if source.get(field) and not target.get(field):
            target[field] = source[field]
    for flag in ("owned", "store_catalog", "store_installed"):
        # Presence, not truthiness: a rescanned install that vanished must be
        # able to clear store_installed so the game becomes Missing.
        if flag in source and source[flag] is not None:
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
    try:
        enqueue_import_media(result["additions"], result["settings"])
    except (OSError, ValueError):
        LOGGER.exception("Could not queue media for the imported folder")
    # Lifecycle event (F1e): a folder scan finished. game_added for the new
    # entries is emitted separately by the update_state diff.
    emit_plugin_event(DATA.parent / "plugins", "scan_finished", {
        "folder": str(folder),
        "added": len(result["additions"]),
        "scanned": len(candidates),
    })
    return len(result["additions"]), len(candidates), result["recommendations"]


def merge_imported_games(imported, identity_fn):
    result = {"added": 0, "found": 0}
    tracked_identities = set()
    tracked_ids = set()

    def track(game):
        tracked_identities.add(identity_fn(game))
        game_id = str(game.get("game_id") or "").strip()
        if game_id:
            tracked_ids.add(game_id)

    def mutate(state):
        filtered = filter_imported(imported, state)
        exact, cross = _index_existing_games(state["games"])
        legacy_existing = {identity_fn(game) for game in state["games"]}
        new_games = []
        timestamp = datetime.now().isoformat(timespec="seconds")
        default_progress = state.get("settings", {}).get("progress_on_first_play", "Playing")
        for game in filtered:
            # Keep caller-owned importer records detached from the committed
            # state.  The warm state store retains the transaction object, so
            # appending this dict directly would leak generated fields such as
            # ``game_id`` back into the importer and affect later identity
            # decisions.
            game = copy.deepcopy(game)
            source_keys = source_identities(game)
            target = next((exact[key] for key in source_keys if key in exact), None)
            tracked_identities.add(identity_fn(game))
            if target is not None or identity_fn(game) in legacy_existing:
                if target is not None:
                    _merge_imported_game(target, game, add_launcher=False)
                    track(target)
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
                track(target)
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

    committed = update_state(mutate)
    if tracked_identities and isinstance(committed, dict):
        queued_games = []
        for game in committed.get("games", []) or []:
            game_id = str(game.get("game_id") or "").strip()
            if game_id in tracked_ids or identity_fn(game) in tracked_identities:
                queued_games.append(game)
        if queued_games:
            try:
                enqueue_import_media(queued_games, committed.get("settings") or {})
            except (OSError, ValueError):
                LOGGER.exception("Could not queue media for imported games")
    return result["added"], result["found"]


def enqueue_import_media(games, settings=None, *, queue_path=None):
    """Queue provider media for imported games using the configured kinds."""
    settings = settings or {}
    queue_path = Path(queue_path) if queue_path else DATA.parent / "media-queue.json"
    media_types = sorted(media_types_from_settings(settings))
    try:
        limit = int(settings.get("media_download_limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    queued_games = 0
    for game in games or []:
        if limit and queued_games >= limit:
            break
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "").strip()
        if not game_id:
            continue
        launchbox_id = game.get("launchbox_db_id")
        provider = str(game.get("metadata_provider") or "").strip()
        if not launchbox_id and not provider:
            continue
        source = provider or "launchbox"
        if source not in MEDIA_QUEUE_BUILTIN_SOURCES and not source.startswith("plugin:"):
            LOGGER.warning("Skipping unsupported media source for %s: %s", game_id, source)
            continue
        try:
            enqueue_media_job(queue_path, {
                "name": game.get("name"),
                "path": game.get("path"),
                "game_id": game_id,
                "source": source,
                "media": media_types,
            })
        except (OSError, ValueError) as error:
            LOGGER.warning("Could not queue media for %s: %s", game_id, error)
            if "queue is full" in str(error).casefold():
                break
            continue
        queued_games += 1
    return queued_games


_MEDIA_WORKER = {
    "thread": None,
    "cancel": None,
    "pause": None,
    "completed": 0,
    "failed": 0,
}


def _media_worker_progress(event):
    if isinstance(event, dict):
        if event.get("status") == "completed":
            _MEDIA_WORKER["completed"] += 1
        elif event.get("status") == "failed":
            _MEDIA_WORKER["failed"] += 1
    try:
        from pkg.state.sse import broadcast_event

        broadcast_event("media-queue", event)
    except Exception:  # noqa: BLE001 - progress must never break the worker
        pass


def start_media_queue_worker(cancel_event=None):
    """Start the durable media queue worker after the auto-import worker."""
    thread = _MEDIA_WORKER.get("thread")
    if thread is not None and thread.is_alive():
        return thread
    cancel = cancel_event or threading.Event()
    pause = threading.Event()
    _MEDIA_WORKER.update({"cancel": cancel, "pause": pause, "completed": 0, "failed": 0})
    thread = threading.Thread(
        target=media_queue_worker,
        kwargs={
            "cancel_event": cancel,
            "pause_event": pause,
            "progress": _media_worker_progress,
            "queue_path": DATA.parent / "media-queue.json",
        },
        name="media-queue",
        daemon=True,
    )
    _MEDIA_WORKER["thread"] = thread
    thread.start()
    return thread


def media_queue_worker_state():
    """Runtime status for the media queue routes (no thread joins)."""
    thread = _MEDIA_WORKER.get("thread")
    pause = _MEDIA_WORKER.get("pause")
    cancel = _MEDIA_WORKER.get("cancel")
    return {
        "running": bool(thread is not None and thread.is_alive()),
        "paused": bool(pause is not None and pause.is_set()),
        "cancelled": bool(cancel is not None and cancel.is_set()),
        "completed": int(_MEDIA_WORKER.get("completed") or 0),
        "failed": int(_MEDIA_WORKER.get("failed") or 0),
    }


def pause_media_queue_worker():
    pause = _MEDIA_WORKER.get("pause")
    if pause is not None:
        pause.set()
    return media_queue_worker_state()


def resume_media_queue_worker():
    pause = _MEDIA_WORKER.get("pause")
    cancel = _MEDIA_WORKER.get("cancel")
    if pause is not None:
        pause.clear()
    if cancel is not None:
        cancel.clear()
    if _MEDIA_WORKER.get("thread") is None or not _MEDIA_WORKER["thread"].is_alive():
        start_media_queue_worker()
    return media_queue_worker_state()


def cancel_media_queue_worker():
    cancel = _MEDIA_WORKER.get("cancel")
    pause = _MEDIA_WORKER.get("pause")
    if cancel is not None:
        cancel.set()
    if pause is not None:
        pause.clear()
    return media_queue_worker_state()


def auto_import_worker(cancel_event=None):
    delay = 10
    # A tick that imported nothing backs off instead of retrying at the floor.
    # The scan cost is real -- a Steam import walks the whole library, and each
    # provider merge runs a transaction -- and on a default install with one
    # watch folder almost every tick finds nothing. Backing off keeps a
    # newly-dropped ROM from waiting minutes, while an idle install stops
    # paying for the tick at all. The state store now also skips the write when
    # a mutation changes nothing, so an idle tick is cheap; this is about not
    # doing the scan in the first place.
    idle_delay = 10
    while not WATCH_STOP.wait(delay):
        if cancel_event and cancel_event.is_set():
            return
        try:
            state = load_state()
        except Exception as error:
            LOGGER.exception("Automatic import paused because library state could not be read: %s", error)
            delay = min(delay * 2, 300)
            continue
        settings = state.get("settings", {})
        folders = settings.get("watch_folders", [])
        found = 0
        for folder in folders:
            try:
                added, _scanned, _recommendations = import_folder_path(folder)
                found += int(added or 0)
            except (OSError, ValueError) as error:
                LOGGER.warning("Watched-folder import failed for %s: %s", folder, error)
        storefront = settings.get("storefront_auto_import", {})
        excluded_platforms = auto_import_excluded_platforms(settings)

        def source_config(key, storefront=storefront):
            value = storefront.get(key, {})
            if isinstance(value, bool):
                return {"enabled": value, "platform": "", "include_uninstalled": False}
            return value if isinstance(value, dict) else {}

        def configured_import(key, importer, identity, excluded_platforms=excluded_platforms):
            nonlocal found
            config = source_config(key)
            if not config.get("enabled"):
                return
            try:
                imported = importer()
                platform = str(config.get("platform") or "").strip()[:80]
                if platform:
                    for game in imported:
                        game["platform"] = platform
                if not config.get("include_uninstalled"):
                    imported = [game for game in imported if game.get("store_installed") is not False]
                imported = [
                    game for game in imported
                    if str(game.get("platform") or "").strip().casefold() not in excluded_platforms
                ]
                found += merge_imported_games(imported, identity)[0]
            except (OSError, ValueError, GameyfinError) as error:
                LOGGER.warning("%s auto-import failed: %s", key, error)

        configured_import("steam", import_steam, lambda game: ("steam", str(game.get("steam_app_id", ""))))
        configured_import(
            "heroic", import_heroic,
            lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", ""))),
        )
        configured_import("lutris", import_lutris, lambda game: ("lutris", str(game.get("lutris_id", ""))))
        if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
            from pkg.parity.parity_storefront_windows import import_ea, import_gog, import_ubisoft

            configured_import("gog", import_gog, lambda game: ("gog", str(game.get("gog_id", ""))))
            configured_import("ea", import_ea, lambda game: ("ea", str(game.get("ea_id", ""))))
            configured_import("ubisoft", import_ubisoft, lambda game: ("ubisoft", str(game.get("ubisoft_id", ""))))
        if source_config("gameyfin").get("enabled"):
            try:
                config = source_config("gameyfin")
                catalog, _providers = catalog_gameyfin(settings)
                imported = catalog_entries_to_games(
                    catalog, installed_only=not config.get("include_uninstalled")
                )
                platform = str(config.get("platform") or "").strip()[:80]
                if platform:
                    for game in imported:
                        game["platform"] = platform
                imported = [
                    game for game in imported
                    if str(game.get("platform") or "").strip().casefold() not in excluded_platforms
                ]
                found += merge_imported_games(
                    imported, lambda game: ("gameyfin", str(game.get("gameyfin_id", "")))
                )[0]
            except (OSError, ValueError, GameyfinError) as error:
                LOGGER.warning("Gameyfin auto-import failed: %s", error)
        for key in storefront:
            plugin_id = plugin_storefront_id(key)
            if not plugin_id:
                continue
            config = source_config(key)
            if not config.get("enabled"):
                continue
            try:
                from pkg.parity.parity_plugin_storefront import plugin_storefront_import

                imported = plugin_storefront_import(plugin_id)
                platform = str(config.get("platform") or "").strip()[:80]
                if platform:
                    for game in imported:
                        game["platform"] = platform
                if not config.get("include_uninstalled"):
                    imported = [game for game in imported if game.get("store_installed") is not False]
                imported = [
                    game for game in imported
                    if str(game.get("platform") or "").strip().casefold() not in excluded_platforms
                ]
                found += merge_imported_games(
                    imported, lambda game, plugin_id=plugin_id: (
                        "plugin", plugin_id, str(game.get("game_id") or "")
                    )
                )[0]
            except (OSError, ValueError) as error:
                LOGGER.warning("%s auto-import failed: %s", key, error)
        for config in list_scan_configs(state):
            if not config.get("auto_update"):
                continue
            folder = str(config.get("folder", "")).strip()
            if not folder:
                continue
            try:
                imported = scan_emulator_folder(
                    folder,
                    emulator_id=str(config.get("emulator_id", "")).strip() or None,
                )
                found += merge_imported_games(
                    imported, lambda game: ("path", str(game.get("path", "")))
                )[0]
            except (OSError, ValueError) as error:
                LOGGER.warning("Emulator scan auto-update failed for %s: %s", folder, error)

        # Back off only when the whole tick found nothing, and return to the
        # floor as soon as it does, so a new ROM is still picked up promptly.
        delay = 10 if found else min(idle_delay * 2, 300)
        idle_delay = delay


STOREFRONT_SCAN_TIMEOUT = 30
STOREFRONT_SOURCES = ("steam", "heroic", "lutris", "gameyfin")
_STOREFRONT_STATUS_FILE = "storefront-status.json"
_STOREFRONT_NAMES = {"steam": "Steam", "heroic": "Heroic", "lutris": "Lutris", "gameyfin": "Gameyfin"}


def plugin_storefront_id(source):
    """Return the plugin id for a ``plugin:<id>`` source, or ``""``."""
    value = str(source or "").strip()
    return value.split(":", 1)[1] if value.startswith("plugin:") and len(value) > 7 else ""


def storefront_plugin_map(plugins_dir=None):
    """Return enabled storefront plugins keyed by source id."""
    from pkg.parity.parity_plugin_storefront import list_storefront_plugins

    root = Path(plugins_dir) if plugins_dir is not None else DATA.parent / "plugins"
    try:
        items = list_storefront_plugins(root)
    except (OSError, ValueError):
        return {}
    return {f"plugin:{item['plugin_id']}": item for item in items}


def storefront_source_ids(plugins_dir=None):
    """Return built-in storefront ids plus enabled plugin storefronts."""
    return (*STOREFRONT_SOURCES, *storefront_plugin_map(plugins_dir))


def _storefront_source_present(source, settings, home=None, plugin_sources=None):
    home = home or Path.home()
    if source == "steam":
        return any((root / "steamapps").is_dir() for root in steam_roots(home))
    if source == "heroic":
        return any(base.is_dir() for base in heroic_bases(home))
    if source == "lutris":
        return bool(shutil.which("lutris") or shutil.which("flatpak"))
    if source == "gameyfin":
        return bool(str(settings.get("gameyfin_url") or "").strip())
    if plugin_storefront_id(source):
        return source in (plugin_sources or {})
    return False


def _storefront_catalog_job(source, settings, include_uninstalled, plugins_dir, result_queue):
    """Spawn-safe child: never returns non-plain data and never mutates state."""
    try:
        plugin_id = plugin_storefront_id(source)
        if plugin_id:
            from pkg.parity.parity_plugin_storefront import plugin_storefront_import

            games = plugin_storefront_import(
                plugin_id,
                installed_only=not include_uninstalled,
                plugins_dir=plugins_dir,
            )
            result_queue.put({"games": games, "error": ""})
            return
        entries = storefront_catalog(source, settings=settings)
        result_queue.put({"entries": entries, "error": ""})
    except Exception as error:  # child boundary: report, do not traceback into the queue
        result_queue.put({"entries": [], "games": [], "error": str(error)[:500]})


def _call_with_timeout(fn, args, timeout=STOREFRONT_SCAN_TIMEOUT):
    """Run a top-level callable in a child process, with a hard wall clock.

    Read the queue before joining: a catalog large enough to fill the pipe would
    otherwise deadlock the parent while it waits for the child to exit.
    """
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=fn, args=(*args, result_queue), daemon=True)
    process.start()
    try:
        payload = result_queue.get(timeout=timeout)
    except queue.Empty:
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(2)
        raise TimeoutError(f"Storefront scan timed out after {timeout}s.") from None
    finally:
        result_queue.close()
        result_queue.join_thread()
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(2)
    return payload


def _source_game_identity(source, game):
    plugin_id = plugin_storefront_id(source)
    if plugin_id:
        return ("plugin", plugin_id, str(game.get("game_id") or ""))
    if source == "steam":
        return ("steam", str(game.get("steam_app_id") or ""))
    if source == "heroic":
        return ("heroic", str(game.get("source") or ""), str(game.get("heroic_app_id") or ""))
    if source == "lutris":
        return ("lutris", str(game.get("lutris_id") or ""))
    return ("gameyfin", str(game.get("gameyfin_id") or ""))


def _storefront_status_path():
    return DATA.parent / _STOREFRONT_STATUS_FILE


def read_storefront_status():
    try:
        payload = json.loads(_storefront_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sources": []}
    return payload if isinstance(payload, dict) and isinstance(payload.get("sources"), list) else {"sources": []}


def _write_storefront_status(sources):
    from backend_io import atomic_write_text
    atomic_write_text(_storefront_status_path(), json.dumps({"sources": sources}, indent=2) + "\n", mode=0o600)


def storefront_scan_worker(cancel_event=None, sources=None, include_uninstalled=True, timeout=STOREFRONT_SCAN_TIMEOUT):
    """Refresh installed state for every configured storefront without deleting games.

    Each source is scanned in a child process. A timeout or provider error records
    that source and leaves the library untouched; earlier sources stay applied.
    """
    state = load_state()
    settings = state.get("settings", {})
    plugins_dir = DATA.parent / "plugins"
    plugin_sources = storefront_plugin_map(plugins_dir)
    known = (*STOREFRONT_SOURCES, *plugin_sources)
    requested = [str(source).casefold() for source in (sources or known)]
    unknown = [source for source in requested if source not in known]
    if unknown:
        raise ValueError(f"Unknown storefront source: {unknown[0]}")
    def pending_status(source):
        plugin_id = plugin_storefront_id(source)
        plugin = plugin_sources.get(source, {}) if plugin_id else {}
        return {
            "id": source,
            "name": plugin.get("name") or _STOREFRONT_NAMES.get(source, source),
            "status": "pending",
            "installed": False,
            "last_scan": datetime.now().isoformat(timespec="seconds"),
            "error": "",
            "games": 0,
        }

    summary = []
    pending = [pending_status(source) for source in requested]
    _write_storefront_status(pending)
    for index, source in enumerate(requested):
        if cancel_event and cancel_event.is_set():
            break
        plugin_id = plugin_storefront_id(source)
        status = pending[index]
        def persist(status=status, remaining=pending[index + 1:]):
            current = summary if status in summary else summary + [status]
            _write_storefront_status(current + remaining)
        present = _storefront_source_present(source, settings, plugin_sources=plugin_sources)
        status["installed"] = present
        library_has_source = any(
            (isinstance(game, dict) and (
                (source == "steam" and game.get("steam_app_id"))
                or (source == "heroic" and game.get("heroic_app_id"))
                or (source == "lutris" and game.get("lutris_id"))
                or (source == "gameyfin" and game.get("gameyfin_id"))
                or (plugin_id and (
                    game.get("plugin_source") == plugin_id
                    or str(game.get("game_id") or "").startswith(f"plugin:{plugin_id}:")
                ))
            ))
            for game in state.get("games", [])
        )
        if not present and not library_has_source:
            status["status"] = "skipped"
            summary.append(status)
            persist()
            continue
        status["status"] = "running"
        persist()
        try:
            payload = _call_with_timeout(
                _storefront_catalog_job,
                (source, settings, include_uninstalled, plugins_dir),
                timeout=timeout,
            )
        except TimeoutError as error:
            status.update({"status": "timeout", "error": str(error)})
            summary.append(status)
            persist()
            continue
        if payload.get("error"):
            status.update({"status": "error", "error": str(payload["error"])[:500]})
            summary.append(status)
            persist()
            continue
        if plugin_id:
            imported = payload.get("games") or []
            if not include_uninstalled:
                imported = [game for game in imported if game.get("store_installed") is not False]
            candidates = [game for game in imported if isinstance(game, dict)]
        else:
            entries = payload.get("entries") or []
            if not include_uninstalled:
                entries = [entry for entry in entries if entry.get("installed")]
            candidates = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                converted = catalog_entries_to_games([entry])
                if not converted:
                    continue
                candidates.append(converted[0])
        update_map = {
            _source_game_identity(source, game): game
            for game in candidates
            if _source_game_identity(source, game)[-1]
        }
        changed = 0

        def mutate(current, source=source, update_map=update_map):
            nonlocal changed
            for game in current.get("games", []):
                if not isinstance(game, dict):
                    continue
                fresh = update_map.get(_source_game_identity(source, game))
                if not fresh:
                    continue
                for field in ("store_installed", "install_dir", "path", "launch", "install_uri", "repair_uri"):
                    if field in fresh:
                        game[field] = fresh[field]
                changed += 1

        update_state(mutate)
        status.update({"status": "ok" if changed else "skipped", "games": changed})
        summary.append(status)
        persist()
    _write_storefront_status(summary)
    return {"sources": summary, "checked": len(summary)}


def auto_import_excluded_platforms(settings):
    values = settings.get("auto_import_excluded_platforms", [])
    return {str(value).strip().casefold() for value in values if str(value).strip()} if isinstance(values, list) else set()


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
            # Merge against `current`, not against the snapshot taken before the
            # network read. A file sync over a network folder takes seconds to
            # minutes; a finish_session increment landing in that window was
            # overwritten by the stale value here, and the playtime was lost.
            #
            # The merge is monotonic -- counters take the max, last_played takes
            # the newer, progress/rating/favorite fill only when empty -- so it
            # is safe to re-apply here: `source` already holds
            # max(snapshot, cloud), and `current` is never below the snapshot, so
            # the result is max(current, cloud). A session played during the sync
            # now wins instead of being discarded.
            apply_synced_stats(game, source)
        current.setdefault("settings", {})["last_cloud_sync"] = result["synced_at"]

    update_state(mutate)
    return result
