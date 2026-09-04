"""Cache structures and cached projection builders for OpenBox library state."""

from collections import OrderedDict
import copy
from dataclasses import dataclass, field
import gzip
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

import openbox
from openbox import DATA, load_state, load_state_readonly, update_state_with_result
from parity_discovery import clear_discovery_cache, discovery_lists
from parity_emulator_defs import list_scan_configs
from parity_filter_presets import list_presets
from parity_gamescope import is_gamescope_guest, list_gamescope_presets, is_mangohud_available
from parity_import_policy import list_exclusions
from parity_integrations import load_emumovies_credentials
from parity_media import REGION_PRIORITY_DEFAULT, active_video, media_types_from_settings
from parity_premium import LIST_COLUMNS_DEFAULT, category_for_platform, custom_field_defs, list_media_packs, platform_categories, strings_for
from parity_saves import games_with_saves
from parity_save_tools import save_tool_status
from plugins import run_plugins
from retroachievements import load_credentials as load_ra_credentials
from updates import VERSION

GZIP_THRESHOLD = 1024
LOGGER = logging.getLogger("openbox")
STATE_LOCK = threading.Lock()

FILE_PROBE_LOCK = threading.Lock()
FILE_PROBE_TTL = 120.0
FILE_PROBE_MAX = 20000
_KNOWN_MEDIA_MAX = 100000

PLUGIN_LIBRARY_TTL = 30.0
PLUGIN_LIBRARY_LOCK = threading.Lock()
_PLUGIN_REFRESH_IN_PROGRESS = {"value": False}

MEDIA_EPOCH = {"value": 0}
MEDIA_EPOCH_LOCK = threading.Lock()
PLUGIN_EPOCH = {"value": 0}

PUBLIC_STATE_LOCK = threading.Lock()
PUBLIC_SETTINGS_LOCK = threading.Lock()

# Available locales for the i18n system (1.7.2).
AVAILABLE_LOCALES = [
    {"code": "en", "name": "English", "native": "English"},
    {"code": "es", "name": "Spanish", "native": "Español"},
    {"code": "de", "name": "German", "native": "Deutsch"},
    {"code": "fr", "name": "French", "native": "Français"},
    {"code": "pt", "name": "Portuguese", "native": "Português"},
]

_KNOWN_MEDIA_SET_LOCK = threading.Lock()

_GAME_PROJECTION_LOCK = threading.Lock()
_GAME_PROJECTION_MAX = 100000

_SANITIZE_MEDIA_PATH_LOCK = threading.Lock()
_SANITIZE_MEDIA_PATH_MAX = 50000

_PLATFORM_CATEGORY_CACHE = {}
_PLATFORM_CATEGORY_LOCK = threading.Lock()
_PLATFORM_CATEGORY_MAX = 5000

STATE_VIEW_LOCK = threading.Lock()

FACET_CACHE_MAX = 64
FACET_BUDGET_MS = 50.0
FACET_DEGRADED = "DEGRADED"


class FacetCache:
    """LRU facet cache with time.monotonic budget and epoch tracking.

    - LRU eviction when max_size exceeded.
    - Budget via time.monotonic(): if facet aggregation exceeds budget_ms,
      return partial facets with degraded=True and code=DEGRADED.
    - Epoch bumps on CACHE_EPOCH._invalidate_all().
    """

    def __init__(self, max_size: int = FACET_CACHE_MAX, budget_ms: float = FACET_BUDGET_MS):
        self.max_size = int(max_size)
        self.budget_ms = float(budget_ms)
        self._store: OrderedDict = OrderedDict()
        self.epoch: int = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._store:
                value = self._store.pop(key)
                self._store[key] = value
                return value
            return None

    def set(self, key, value):
        with self._lock:
            if key in self._store:
                self._store.pop(key)
            self._store[key] = value
            if len(self._store) > self.max_size:
                self._store.popitem(last=False)

    def clear(self):
        with self._lock:
            self._store.clear()
            self.epoch += 1

    def _explorer_counts(self, games, field: str, start: float, effective: float):
        """Count facets with budget check every iteration. Returns (counts, degraded)."""
        counts: dict[str, int] = {}
        degraded = False
        for game in games:
            # budget check before processing next game
            if (time.monotonic() - start) * 1000.0 > effective:
                degraded = True
                break
            if not isinstance(game, dict):
                continue
            if game.get("hidden"):
                continue
            if field == "genre":
                for part in str(game.get("genre", "")).split(","):
                    label = part.strip()
                    if label:
                        counts[label] = counts.get(label, 0) + 1
            elif field == "developer":
                label = str(game.get("developer", "")).strip()
                if label:
                    counts[label] = counts.get(label, 0) + 1
            elif field == "publisher":
                label = str(game.get("publisher", "")).strip()
                if label:
                    counts[label] = counts.get(label, 0) + 1
            elif field == "platform":
                label = str(game.get("platform", "Unspecified")).strip() or "Unspecified"
                counts[label] = counts.get(label, 0) + 1
            elif field == "progress":
                label = str(game.get("progress", "")).strip() or "Unset"
                counts[label] = counts.get(label, 0) + 1
            elif field == "esrb":
                label = str(game.get("esrb", "")).strip() or "Unrated"
                counts[label] = counts.get(label, 0) + 1
        return counts, degraded

    def compute_facets(self, games, field: str, limit: int = 40, budget_ms: float | None = None):
        """Compute facets with caching and budget. Returns dict with facets/degraded/code/epoch."""
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 40
        if field not in {"genre", "developer", "publisher", "platform", "progress", "esrb"}:
            return {"facets": [], "degraded": False, "code": "OK", "epoch": self.epoch}
        # cache key includes epoch so invalidation naturally misses
        # use len + first/last ids for fingerprint to avoid collisions on same len
        try:
            first_id = str(games[0].get("game_id") or games[0].get("id") or "") if games else ""
            last_id = str(games[-1].get("game_id") or games[-1].get("id") or "") if games else ""
        except Exception:
            first_id = ""
            last_id = ""
        key = (field, limit, len(games), first_id, last_id, self.epoch)
        cached = self.get(key)
        if cached is not None:
            return cached
        effective = float(budget_ms) if budget_ms is not None else float(self.budget_ms)
        start = time.monotonic()
        counts, degraded = self._explorer_counts(games, field, start, effective)
        # final budget check if not already degraded but over budget after loop
        if not degraded and (time.monotonic() - start) * 1000.0 > effective:
            degraded = True
        items = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
        facets = [{"value": value, "count": count} for value, count in items[:limit]]
        result = {"facets": facets, "degraded": degraded, "code": FACET_DEGRADED if degraded else "OK", "epoch": self.epoch}
        self.set(key, result)
        return result

    def get_facets(self, games, field: str, limit: int = 40, budget_ms: float | None = None):
        """Alias for compute_facets."""
        return self.compute_facets(games, field, limit=limit, budget_ms=budget_ms)


FACET_CACHE = FacetCache()


def _ns(name, default):
    mod = sys.modules.get("webapp_state")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    from pkg.state._deps import get
    return get(name, default)


def clear_file_probe_cache():
    with FILE_PROBE_LOCK:
        FILE_PROBE_CACHE.clear()


def _fast_realpath(value):
    """Fast equivalent of str(Path(value).resolve(strict=False))."""
    return os.path.realpath(os.path.expanduser(value))


def _media_dir_mtime():
    """Compute a combined mtime fingerprint for all media roots.

    Walks subdirectories to detect file additions/removals, which change
    the parent directory mtime but not the root mtime.
    """
    from pkg.state.media_probe import MEDIA_ROOTS_ENV

    data_parent = _ns("DATA", DATA).parent
    combined = 0.0
    try:
        media_dir = data_parent / "media"
        if media_dir.is_dir():
            for dirpath, _dirnames, _filenames in os.walk(str(media_dir)):
                try:
                    combined += os.stat(dirpath).st_mtime
                except OSError:
                    pass
    except OSError:
        pass
    env_value = os.environ.get(MEDIA_ROOTS_ENV, "")
    if env_value:
        for item in env_value.split(os.pathsep):
            item = item.strip()
            if not item:
                continue
            try:
                root = Path(item).expanduser()
                if root.is_dir():
                    for dirpath, _dirnames, _filenames in os.walk(str(root)):
                        try:
                            combined += os.stat(dirpath).st_mtime
                        except OSError:
                            pass
            except OSError:
                pass
    return combined


def _build_known_media_set():
    """Pre-scan all media roots into a set of resolved absolute paths.

    One fast directory walk replaces thousands of individual stat calls in
    _build_public_state for large libraries. Results are memoized keyed
    on the media epoch, and invalidated by bump_media_epoch().
    """
    from pkg.state.media_probe import MEDIA_ROOTS_ENV

    cache_key = (MEDIA_EPOCH["value"], os.environ.get(MEDIA_ROOTS_ENV, ""))
    with _KNOWN_MEDIA_SET_LOCK:
        if _KNOWN_MEDIA_SET_CACHE.get("key") == cache_key and _KNOWN_MEDIA_SET_CACHE.get("result") is not None:
            return _KNOWN_MEDIA_SET_CACHE["result"]
    known = set()
    capped = False

    def _scan_dir(dir_path):
        nonlocal capped
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            known.add(entry.path)
                            known.add(os.path.realpath(entry.path))
                        elif entry.is_dir(follow_symlinks=False):
                            _scan_dir(entry.path)
                    except OSError:
                        pass
                    if len(known) > _KNOWN_MEDIA_MAX:
                        capped = True
                        break
        except OSError:
            pass

    data_parent = _ns("DATA", DATA).parent
    try:
        media_dir = data_parent / "media"
        if media_dir.is_dir():
            _scan_dir(str(media_dir))
    except OSError:
        pass
    env_value = os.environ.get(MEDIA_ROOTS_ENV, "")
    if env_value:
        for item in env_value.split(os.pathsep):
            item = item.strip()
            if not item:
                continue
            try:
                root = Path(item).expanduser()
                if root.is_dir():
                    _scan_dir(str(root))
                if capped:
                    break
            except OSError:
                pass
    if capped:
        LOGGER.warning("Media set capped at %d entries; some media files may not be detected", _KNOWN_MEDIA_MAX)
    with _KNOWN_MEDIA_SET_LOCK:
        _KNOWN_MEDIA_SET_CACHE.update({"key": cache_key, "result": known, "mtime_key": None})
    return known


def _media_set_contains(media_set, path_value):
    """O(1) check for whether a sanitized media path is in the pre-scanned set."""
    if not media_set or not path_value:
        return False
    p_str = str(path_value)
    if p_str in media_set:
        return True
    if p_str.startswith("/"):
        norm = os.path.normpath(p_str)
        if norm in media_set:
            return True
    return False


@dataclass
class CacheEpoch:
    """Centralized cache epoch for coordinated invalidation (ADR-0005).

    Owns every mutable cache dict so that a single ``_invalidate_all`` call
    clears them atomically, eliminating the risk of missed invalidation when
    new caches are added.
    """
    media: int = 0
    plugin: int = 0
    state: dict = field(default_factory=lambda: {
        "signature": None, "payload": None, "raw": None,
        "raw_gzip": None, "games_by_id": None,
    })
    settings: dict = field(default_factory=lambda: {
        "signature": None, "payload": None,
    })
    media_set: dict = field(default_factory=lambda: {
        "key": None, "result": None, "mtime_key": None,
    })
    game_projection: dict = field(default_factory=dict)
    sanitize_media_path: dict = field(default_factory=dict)
    file_probe: dict = field(default_factory=OrderedDict)

    # -- invalidation --------------------------------------------------------

    def _invalidate_all(
        self,
        *,
        bump_media: bool = False,
        bump_plugin: bool = False,
    ) -> None:
        """Atomically clear every cache dict, optionally bumping epoch counters.

        Lock acquisition order matches the original hand-written sequence to
        avoid introducing deadlocks with code that holds a single cache lock.
        """
        if bump_media:
            self.media += 1
            with MEDIA_EPOCH_LOCK:
                MEDIA_EPOCH["value"] += 1
        if bump_plugin:
            self.plugin += 1
            PLUGIN_EPOCH["value"] += 1
        with _KNOWN_MEDIA_SET_LOCK:
            self.media_set.update({"key": None, "result": None, "mtime_key": None})
        with _GAME_PROJECTION_LOCK:
            self.game_projection.clear()
        with _SANITIZE_MEDIA_PATH_LOCK:
            self.sanitize_media_path.clear()
        with PLUGIN_LIBRARY_LOCK:
            PLUGIN_LIBRARY_CACHE.update({"at": 0.0, "payload": None, "state_signature": None})
        with PUBLIC_STATE_LOCK:
            self.state.update({
                "signature": None, "payload": None, "raw": None,
                "raw_gzip": None, "games_by_id": None,
            })
        with PUBLIC_SETTINGS_LOCK:
            self.settings.update({"signature": None, "payload": None})
        with STATE_VIEW_LOCK:
            STATE_VIEW_CACHE.update({"signature": None, "state": None})
        # Facet cache epoch bump and clear
        try:
            FACET_CACHE.clear()
        except NameError:
            pass
        clear_file_probe_cache()


CACHE_EPOCH = CacheEpoch()

# -- module-level aliases (backward compat) ----------------------------------
# These point to the same dict objects owned by CACHE_EPOCH so that existing
# ``from pkg.state.cache import PUBLIC_STATE_CACHE`` imports keep working.
FILE_PROBE_CACHE = CACHE_EPOCH.file_probe
PLUGIN_LIBRARY_CACHE = {"at": 0.0, "payload": None, "state_signature": None}
PUBLIC_STATE_CACHE = CACHE_EPOCH.state
PUBLIC_SETTINGS_CACHE = CACHE_EPOCH.settings
_KNOWN_MEDIA_SET_CACHE = CACHE_EPOCH.media_set
_GAME_PROJECTION_CACHE = CACHE_EPOCH.game_projection
_SANITIZE_MEDIA_PATH_CACHE = CACHE_EPOCH.sanitize_media_path
STATE_VIEW_CACHE = {"signature": None, "state": None}


def bump_media_epoch():
    """Invalidate browser media caches by bumping the version suffix in media URLs."""
    CACHE_EPOCH._invalidate_all(bump_media=True)


def public_settings(state=None):
    load_fn = _ns("load_state", load_state)
    state = state or load_fn()
    sig = openbox.STATE_STORE.signature()
    with PUBLIC_SETTINGS_LOCK:
        if sig and PUBLIC_SETTINGS_CACHE["signature"] == sig and PUBLIC_SETTINGS_CACHE["payload"] is not None:
            return PUBLIC_SETTINGS_CACHE["payload"]
    uncached_fn = _ns("_public_settings_uncached", _public_settings_uncached)
    result = uncached_fn(state)
    # Recheck signature after computing to avoid caching stale data under a new signature
    sig_after = openbox.STATE_STORE.signature()
    if sig_after == sig:
        with PUBLIC_SETTINGS_LOCK:
            PUBLIC_SETTINGS_CACHE.update({"signature": sig, "payload": result})
    return result


def _public_settings_uncached(state):
    from pkg.state.media_probe import sanitize_document_records

    data_parent = _ns("DATA", DATA).parent
    settings = state.get("settings", {})
    platform_documents = settings.get("platform_documents", {})
    if not isinstance(platform_documents, dict):
        platform_documents = {}
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
        "platform_documents": {
            str(platform): sanitize_document_records(documents)
            for platform, documents in platform_documents.items()
        },
        "custom_field_defs": custom_field_defs(settings),
        "platform_categories": platform_categories(settings),
        "list_columns": settings.get("list_columns", list(LIST_COLUMNS_DEFAULT)),
        "library_view": settings.get("library_view", "grid"),
        "cover_grouping": settings.get("cover_grouping", "shape"),
        "locale": settings.get("locale", "en"),
        "available_locales": AVAILABLE_LOCALES,
        "strings": strings_for(settings.get("locale", "en")),
        "attract_mode_seconds": settings.get("attract_mode_seconds", settings.get("screensaver_seconds", 90)),
        "bigbox_startup_video": settings.get("bigbox_startup_video", ""),
        "bigbox_shutdown_commands": settings.get("bigbox_shutdown_commands", []),
        "tray_enabled": settings.get("tray_enabled", False),
        "minimize_to_tray": settings.get("minimize_to_tray", False),
        "media_packs": list_media_packs(settings),
        "controller_prompt_hint": (
            "A Play · B Back · M Menu"
            if settings.get("controller_prompt_hint") is True
            else ""
            if settings.get("controller_prompt_hint") is False
            else str(settings.get("controller_prompt_hint") or "")
        ),
        "controller_prompt_pack": settings.get("controller_prompt_pack", "xbox"),
        "premium_features_free": True,
        "progress_on_first_play": settings.get("progress_on_first_play", "Playing"),
        "tracking_mode": settings.get("tracking_mode", "default"),
        "tracking_delay": settings.get("tracking_delay", 0),
        "tracking_frequency": settings.get("tracking_frequency", 2),
        "tracking_process_name": settings.get("tracking_process_name", ""),
        "apply_perf": settings.get("apply_perf", "auto"),
        "auto_close_store_clients": settings.get("auto_close_store_clients", False),
        "filter_presets": list_presets(state),
        "import_exclusions": list_exclusions(state),
        "emulator_scan_configs": list_scan_configs(state),
        "safe_mode": bool(os.environ.get("OPENBOX_SAFE_MODE")),
        "emumovies_configured": bool(load_emumovies_credentials(data_parent).get("username")),
        "version": VERSION,
        "appimage": bool(os.environ.get("APPIMAGE")),
        "gamescope_guest": is_gamescope_guest(force="--game-mode" in sys.argv),
        "gamescope_presets": list_gamescope_presets(settings.get("gamescope_custom_presets")),
        "mangohud_available": is_mangohud_available(),
        "gamescope_preset": settings.get("gamescope_preset", ""),
        "mangohud_enabled": settings.get("mangohud_enabled", False),
        "mood_match_enabled": settings.get("mood_match_enabled", False),
        "mood_match_bigbox": settings.get("mood_match_bigbox", False),
    }


def _public_state_signature():
    return (openbox.STATE_STORE.signature(), MEDIA_EPOCH["value"], PLUGIN_EPOCH["value"])


def _project_game(game, index, media_set, save_indices, video_priority, settings, media_epoch):
    from pkg.state.media_probe import probe_path, sanitize_document_records, sanitize_media_path

    san_med = _ns("sanitize_media_path", sanitize_media_path)
    prb_pth = _ns("probe_path", probe_path)
    san_doc = _ns("sanitize_document_records", sanitize_document_records)
    med_cnt = _ns("_media_set_contains", _media_set_contains)

    ckey = (
        id(game), index, media_epoch,
        game.get("favorite"), game.get("hidden"), game.get("hide_in_bigbox"),
        game.get("last_played"), game.get("play_count"), game.get("playtime_seconds"),
        game.get("progress"), game.get("rating"), game.get("notes"),
        game.get("name"), game.get("path"), game.get("cover"), game.get("background"),
        game.get("platform"), index in save_indices,
    )
    with _GAME_PROJECTION_LOCK:
        cached = _GAME_PROJECTION_CACHE.get(ckey)
        if cached is not None:
            return cached

    cov = san_med(game.get("cover", ""))
    bg = san_med(game.get("background", ""))
    logo = san_med(game.get("clear_logo", ""))
    fanart = san_med(game.get("fanart", ""))
    banner = san_med(game.get("banner", ""))
    icon = san_med(game.get("icon", ""))
    bback = san_med(game.get("box_back", ""))
    bspine = san_med(game.get("box_spine", ""))
    b3d = san_med(game.get("box_3d", ""))
    tscreen = san_med(game.get("title_screen", ""))
    cfront = san_med(game.get("cart_front", ""))
    cback = san_med(game.get("cart_back", ""))
    disc = san_med(game.get("disc", ""))
    ad = san_med(game.get("advertisement", ""))
    man = san_med(game.get("manual", ""))
    mus = san_med(game.get("music", ""))

    has_cov = med_cnt(media_set, cov)
    vfield, vpath = active_video(dict(game), video_priority)
    vpath_clean = san_med(vpath) if vpath else ""
    if not vpath_clean:
        vfield = ""

    raw_path = game.get("path", "")
    path_exists = prb_pth(str(raw_path), file_only=False) if raw_path else False
    store_installed = bool(game["store_installed"]) if "store_installed" in game else path_exists

    game_id = game.get("game_id", "")
    steam_id = game.get("steam_app_id", "")
    heroic_id = game.get("heroic_app_id", "")
    lutris_id = game.get("lutris_id", "")
    gameyfin_id = game.get("gameyfin_id", "")
    store_catalog = bool(game.get("store_catalog"))

    alt = game.get("alternate_names", [])
    if not isinstance(alt, list):
        alt = [n.strip() for n in str(alt or "").split(";") if n.strip()]

    custom = game.get("custom_fields", {})
    if not isinstance(custom, dict):
        custom = {}

    tags = game.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    documents = san_doc(game.get("documents", []))
    raw_screenshots = game.get("screenshots", [])
    if not isinstance(raw_screenshots, list):
        raw_screenshots = []
    screenshots = [safe_path for p in raw_screenshots for safe_path in [san_med(p)] if safe_path]
    available_screenshots = [i for i, p in enumerate(screenshots) if med_cnt(media_set, p)]

    platform = str(game.get("platform", ""))
    settings_id = id(settings)
    max_cat = _ns("_PLATFORM_CATEGORY_MAX", _PLATFORM_CATEGORY_MAX)
    with _PLATFORM_CATEGORY_LOCK:
        cat_key = (platform, settings_id)
        platform_cat = _PLATFORM_CATEGORY_CACHE.get(cat_key)
        if platform_cat is None:
            platform_cat = category_for_platform(platform, settings)
            if len(_PLATFORM_CATEGORY_CACHE) >= max_cat:
                _PLATFORM_CATEGORY_CACHE.clear()
            _PLATFORM_CATEGORY_CACHE[cat_key] = platform_cat

    proj = {
        "name": game.get("name", ""),
        "platform": platform,
        "genre": game.get("genre", ""),
        "year": game.get("year", ""),
        "developer": game.get("developer", ""),
        "publisher": game.get("publisher", ""),
        "series": game.get("series", ""),
        "collection": game.get("collection", ""),
        "description": game.get("description", ""),
        "path": raw_path,
        "launch": game.get("launch", ""),
        "launch_profile": game.get("launch_profile", ""),
        "cover": cov,
        "background": bg,
        "clear_logo": logo,
        "fanart": fanart,
        "banner": banner,
        "icon": icon,
        "box_back": bback,
        "box_spine": bspine,
        "box_3d": b3d,
        "title_screen": tscreen,
        "cart_front": cfront,
        "cart_back": cback,
        "disc": disc,
        "advertisement": ad,
        "manual": man,
        "source": game.get("source", ""),
        "steam_app_id": steam_id,
        "lutris_id": lutris_id,
        "install_dir": game.get("install_dir", ""),
        "heroic_app_id": heroic_id,
        "rom_name": game.get("rom_name", ""),
        "clone_of": game.get("clone_of", ""),
        "set_type": game.get("set_type", ""),
        "ra_game_id": game.get("ra_game_id", ""),
        "ra_hash": game.get("ra_hash", ""),
        "launchbox_db_id": game.get("launchbox_db_id", ""),
        "archive_member": game.get("archive_member", ""),
        "video": san_med(game.get("video", "")),
        "music": mus,
        "video_snap": san_med(game.get("video_snap", "")),
        "video_theme": san_med(game.get("video_theme", "")),
        "video_trailer": san_med(game.get("video_trailer", "")),
        "video_recording": san_med(game.get("video_recording", "")),
        "progress": game.get("progress", ""),
        "rating": game.get("rating", ""),
        "notes": game.get("notes", ""),
        "region": game.get("region", ""),
        "play_mode": game.get("play_mode", ""),
        "sort_title": game.get("sort_title", ""),
        "added_at": game.get("added_at", ""),
        "alternate_names": alt,
        "max_players": game.get("max_players", ""),
        "wikipedia_url": game.get("wikipedia_url", ""),
        "video_url": game.get("video_url", ""),
        "hide_in_bigbox": bool(game.get("hide_in_bigbox")),
        "esrb": game.get("esrb", ""),
        "broken": bool(game.get("broken")),
        "portable": bool(game.get("portable")),
        "controller_support": game.get("controller_support", ""),
        "disc_count": game.get("disc_count", ""),
        "gameyfin_id": gameyfin_id,
        "gameyfin_provider": game.get("gameyfin_provider", ""),
        "store_catalog": store_catalog,
        "store_installed": store_installed,
        "owned": bool(game.get("owned") or store_catalog or steam_id or heroic_id or lutris_id or gameyfin_id),
        "tracking_mode": game.get("tracking_mode", ""),
        "tracking_delay": game.get("tracking_delay", ""),
        "tracking_frequency": game.get("tracking_frequency", ""),
        "tracking_process_name": game.get("tracking_process_name", ""),
        "igdb_id": game.get("igdb_id", ""),
        "id": index,
        "game_id": game_id,
        "favorite": bool(game.get("favorite")),
        "hidden": bool(game.get("hidden")),
        "last_played": game.get("last_played", ""),
        "play_count": game.get("play_count", 0),
        "playtime_seconds": game.get("playtime_seconds", 0),
        "path_exists": path_exists,
        "has_cover": has_cov,
        "has_background": med_cnt(media_set, bg),
        "has_clear_logo": med_cnt(media_set, logo),
        "has_fanart": med_cnt(media_set, fanart),
        "has_banner": med_cnt(media_set, banner),
        "has_icon": med_cnt(media_set, icon),
        "has_box_back": med_cnt(media_set, bback),
        "has_box_spine": med_cnt(media_set, bspine),
        "has_box_3d": med_cnt(media_set, b3d),
        "has_title_screen": med_cnt(media_set, tscreen),
        "has_cart_front": med_cnt(media_set, cfront),
        "has_cart_back": med_cnt(media_set, cback),
        "has_disc": med_cnt(media_set, disc),
        "has_advertisement": med_cnt(media_set, ad),
        "has_manual": med_cnt(media_set, man),
        "has_video": bool(vpath_clean),
        "active_video_field": vfield,
        "has_music": med_cnt(media_set, mus),
        "has_saves": index in save_indices or bool(game.get("save_paths")),
        "has_documents": bool(documents),
        "has_versions": bool(game.get("versions")),
        "has_achievements": bool(game.get("ra_game_id")),
        "has_highscores": bool(game.get("rom_name")) and platform.casefold() in {"arcade", "mame", "finalburn neo"},
        "has_missing_media": not has_cov,
        "extract_archive": bool(game.get("extract_archive")),
        "applications": game.get("applications", []),
        "versions": game.get("versions", []),
        "documents": documents,
        "save_paths": game.get("save_paths", []),
        "screenshots": screenshots,
        "available_screenshots": available_screenshots,
        "custom_fields": custom,
        "platform_category": platform_cat,
        "tags": tags,
        "installable": bool(gameyfin_id) and not store_installed,
        "legacy_game_ids": list(game.get("legacy_game_ids", [])) if isinstance(game.get("legacy_game_ids"), list) else [],
    }
    max_proj = _ns("_GAME_PROJECTION_MAX", _GAME_PROJECTION_MAX)
    with _GAME_PROJECTION_LOCK:
        if len(_GAME_PROJECTION_CACHE) >= max_proj:
            _GAME_PROJECTION_CACHE.clear()
        _GAME_PROJECTION_CACHE[ckey] = proj
    return proj


def _build_public_state():
    state_lock = _ns("STATE_LOCK", STATE_LOCK)
    load_ro = _ns("load_state_readonly", load_state_readonly)
    build_known = _ns("_build_known_media_set", _build_known_media_set)
    proj_game = _ns("_project_game", _project_game)
    pub_settings = _ns("public_settings", public_settings)
    run_pl = _ns("run_plugins", run_plugins)
    data_parent = _ns("DATA", DATA).parent

    with state_lock:
        state = load_ro()
        state_signature = openbox.STATE_STORE.signature()
    save_indices = set(games_with_saves(state["games"]))
    media_set = build_known()
    video_priority = state.get("settings", {}).get("video_priority")
    settings = state.get("settings", {})
    media_epoch = MEDIA_EPOCH["value"]
    raw_games = state["games"]

    games = [
        proj_game(game, index, media_set, save_indices, video_priority, settings, media_epoch)
        for index, game in enumerate(raw_games)
    ]
    decorated = games
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        now = time.monotonic()
        cached = PLUGIN_LIBRARY_CACHE
        with PLUGIN_LIBRARY_LOCK:
            if cached["payload"] is not None and cached.get("state_signature") == state_signature:
                # Return cached result immediately; refresh in background if stale
                result = cached["payload"]
                if now - cached["at"] >= PLUGIN_LIBRARY_TTL and not _PLUGIN_REFRESH_IN_PROGRESS["value"]:
                    _PLUGIN_REFRESH_IN_PROGRESS["value"] = True
                    refresh_games = games
                    refresh_signature = state_signature

                    def _refresh_plugins(games_snapshot=refresh_games, expected_signature=refresh_signature):
                        try:
                            fresh = run_pl(data_parent / "plugins", "library", {"games": games_snapshot})
                            with state_lock:
                                if openbox.STATE_STORE.signature() != expected_signature:
                                    return
                                with PLUGIN_LIBRARY_LOCK:
                                    PLUGIN_LIBRARY_CACHE.update({
                                        "at": time.monotonic(),
                                        "payload": fresh,
                                        "state_signature": expected_signature,
                                    })
                                    PLUGIN_EPOCH["value"] += 1
                            with PUBLIC_STATE_LOCK:
                                PUBLIC_STATE_CACHE.update({"signature": None, "payload": None, "raw": None, "raw_gzip": None, "games_by_id": None})
                        except Exception:
                            LOGGER.exception("Background plugin refresh failed")
                        finally:
                            with PLUGIN_LIBRARY_LOCK:
                                _PLUGIN_REFRESH_IN_PROGRESS["value"] = False

                    threading.Thread(target=_refresh_plugins, daemon=True).start()
            else:
                # First call or changed state: block and populate cache
                result = run_pl(data_parent / "plugins", "library", {"games": games})
                cached.update({"at": now, "payload": result, "state_signature": state_signature})
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
        "ra_configured": bool(load_ra_credentials(data_parent)),
        "settings": pub_settings(state),
        "discovery": discovery_lists(state["games"]),
        "media_epoch": media_epoch,
    }


def _public_state_cached():
    build_pub = _ns("_build_public_state", _build_public_state)
    with PUBLIC_STATE_LOCK:
        signature = _public_state_signature()
        if PUBLIC_STATE_CACHE["raw"] is not None and PUBLIC_STATE_CACHE["signature"] == signature:
            return PUBLIC_STATE_CACHE
    payload = build_pub()
    raw = json.dumps(payload).encode()
    raw_gzip = gzip.compress(raw) if len(raw) >= GZIP_THRESHOLD else raw
    games_by_id = {}
    for game in payload["games"]:
        gid = str(game.get("game_id") or "")
        if gid:
            games_by_id[gid] = game
        for leg_id in game.get("legacy_game_ids", []):
            if leg_id:
                games_by_id[str(leg_id)] = game
        games_by_id[str(game.get("id"))] = game
    with PUBLIC_STATE_LOCK:
        if PUBLIC_STATE_CACHE["raw"] is not None and PUBLIC_STATE_CACHE["signature"] == signature:
            return PUBLIC_STATE_CACHE
        PUBLIC_STATE_CACHE.update({
            "signature": signature,
            "payload": payload,
            "raw": raw,
            "raw_gzip": raw_gzip,
            "games_by_id": games_by_id,
        })
        return PUBLIC_STATE_CACHE


def public_state():
    """Return the full library projection, cached until library state changes."""
    cached_fn = _ns("_public_state_cached", _public_state_cached)
    return cached_fn()["payload"]


def public_state_bytes():
    """Return the serialized library projection, cached until library state changes."""
    cached_fn = _ns("_public_state_cached", _public_state_cached)
    return cached_fn()["raw"]


def public_state_etag():
    """Stable ETag for the library projection, derived from its signature."""
    signature = _public_state_signature()
    stat = signature[0] or (0, 0, 0)
    return f'"{stat[0]:x}-{stat[1]:x}-{signature[1]}-{signature[2]}"'


def load_state_view():
    """Read-only library snapshot reused across requests until the file changes."""
    load_ro = _ns("load_state_readonly", load_state_readonly)
    with STATE_VIEW_LOCK:
        signature = openbox.STATE_STORE.signature()
        if STATE_VIEW_CACHE["state"] is not None and STATE_VIEW_CACHE["signature"] == signature:
            return copy.deepcopy(STATE_VIEW_CACHE["state"])
    raw = load_ro()
    detached = copy.deepcopy(dict(raw))
    with STATE_VIEW_LOCK:
        if STATE_VIEW_CACHE["state"] is not None and STATE_VIEW_CACHE["signature"] == signature:
            return copy.deepcopy(STATE_VIEW_CACHE["state"])
        STATE_VIEW_CACHE.update({"signature": signature, "state": detached})
        return copy.deepcopy(detached)


def transact_state(mutator):
    """Run one read-modify-write transaction under the local and process lock."""
    state_lock = _ns("STATE_LOCK", STATE_LOCK)
    upd_res = _ns("update_state_with_result", update_state_with_result)
    with state_lock:
        result = upd_res(mutator)
    CACHE_EPOCH._invalidate_all()
    clear_discovery_cache()
    return result
