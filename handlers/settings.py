"""SettingsHandlers capability handlers. Settings, profiles, performance profiles, and RetroAchievements."""

import copy
import json
import re
from pathlib import Path

from catalog import PROGRESS, clean_currency
from openbox import discover_profiles, load_state, update_state_with_result
from pkg.parity.parity_backup import AUTO_BACKUP_KEEP
from pkg.parity.parity_integrations import inject_retroachievements
from pkg.parity.parity_integration_tokens import clean_tokens
from pkg.parity.parity_media import REGION_PRIORITY_DEFAULT
from pkg.parity.parity_premium import LIST_COLUMNS_DEFAULT, custom_field_defs, enhanced_ra_profile, platform_categories
from pkg.parity.parity_tracking import TRACKING_MODES
from retroachievements import api_get as ra_api_get, game_progress as ra_game_progress, load_credentials as load_ra_credentials, match_game as match_ra_game, save_credentials as save_ra_credentials
from routes.registry import route
from settings_schema import KNOWN_SETTINGS, sanitize_settings
from pkg.platform_compat import IS_WINDOWS
from pkg.state.paths import PathTokenError, normalize_root
from state_store import PATH_MODES
from pkg.parity.parity_steam_achievements import (
    credential_status as steam_credential_status,
    fetch_owned_games,
    load_steam_credentials,
    merge_steam_stats,
    save_steam_credentials,
)
from webapp_state import DATA, JOB_MANAGER, LOGGER, MEDIA_TYPES_ALL, STATE_LOCK, clean_commands, game_from_payload, load_state_view, public_settings, transact_state


def _safe_int(value, default, message):
    """Coerce to int. Stored nulls/containers fall back to default; unparseable
    strings raise a 400-style ValueError instead of leaking a TypeError (500)."""
    if value is None or isinstance(value, (list, dict)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(message) from error


def _safe_float(value, default, message):
    if value is None or isinstance(value, (list, dict)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(message) from error


def _clean_bool(merged, key, default):
    """Return a strict boolean setting. Booleans are never numeric input."""
    value = merged.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be true or false.")


def _clean_enum(merged, key, allowed, default):
    value = merged.get(key, default)
    if value is None:
        return default
    if isinstance(value, str) and value in allowed:
        return value
    choices = ", ".join(sorted(str(item) for item in allowed))
    raise ValueError(f"{key} must be one of: {choices}.")


def _clean_bounded_int(merged, key, default, low, high, message):
    value = merged.get(key, default)
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise ValueError(message)
    number = _safe_int(value, default, message)
    if not low <= number <= high:
        raise ValueError(message)
    return number


def _clean_str_list(merged, key, limit, item_limit):
    value = merged.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of at most {limit} strings.")
    if len(value) > limit:
        raise ValueError(f"{key} must be a list of at most {limit} strings.")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} entries must be strings.")
        text = item.strip()[:item_limit]
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_dict(merged, key, limit):
    value = merged.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object with at most {limit} entries.")
    if len(value) > limit:
        raise ValueError(f"{key} must be an object with at most {limit} entries.")
    return copy.deepcopy(value)


SIDEBAR_SECTION_IDS = ("search", "view", "filters", "platforms", "playlists", "explorer")
PROGRESS_VALUES_DEFAULT = ("Unplayed", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned")
CONTROLLER_ACTIONS = ("play", "back", "favorite", "random", "page_left", "page_right", "pause", "menu")
KEYBOARD_ACTIONS = (
    "play", "back", "favorite", "random", "page_left", "page_right",
    "pause", "menu", "search", "settings", "reader_prev", "reader_next",
)
KEYBOARD_MAP_DEFAULT = {
    "play": "Enter",
    "back": "Escape",
    "favorite": "f",
    "random": "r",
    "page_left": "PageUp",
    "page_right": "PageDown",
    "pause": "p",
    "menu": "m",
    "search": "/",
    "settings": "Ctrl+,",
    "reader_prev": "ArrowLeft",
    "reader_next": "ArrowRight",
}
KEY_SPEC_RE = re.compile(
    r"^(?:(?:Ctrl|Alt|Shift)\+)*(?:Key[A-Z]|Digit[0-9]|F(?:[1-9]|1[0-2])|"
    r"Arrow(?:Left|Right|Up|Down)|Page(?:Up|Down)|Home|End|Enter|Escape|Space|Tab|Backspace|"
    r"[A-Za-z0-9,./;'\[\]\\=-])$"
)


def _clean_sidebar_sections(merged):
    value = merged.get("sidebar_sections", list(SIDEBAR_SECTION_IDS))
    if value is None:
        return []
    # Older builds stored extra section ids (for example favourites and
    # recent). Preserve them so upgrading and saving settings does not erase
    # a user's order; the frontend only reorders sections it renders.
    return _clean_str_list({"sidebar_sections": value}, "sidebar_sections", 20, 40)


def _clean_progress_values(merged):
    value = merged.get("progress_values", list(PROGRESS_VALUES_DEFAULT))
    if value is None:
        return list(PROGRESS_VALUES_DEFAULT)
    if not isinstance(value, list):
        raise ValueError("Progress values must be a list of known progress labels.")
    cleaned = []
    for item in value:
        text = str(item).strip()
        if text not in PROGRESS or not text:
            raise ValueError("Progress values must be a list of known progress labels.")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_watch_folders(merged):
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
    return clean_folders


def _clean_screensaver_seconds(merged):
    seconds = _safe_int(merged.get("screensaver_seconds", 90), 90,
                        "Screensaver delay must be 0 or between 30 and 3600 seconds.")
    if seconds and not 30 <= seconds <= 3600:
        raise ValueError("Screensaver delay must be 0 or between 30 and 3600 seconds.")
    return seconds


def _clean_controller_types(merged):
    default = [
        {"id": "gamepad", "name": "Gamepad", "kind": "gamepad"},
        {"id": "arcade_stick", "name": "Arcade stick", "kind": "arcade_stick"},
        {"id": "racing_wheel", "name": "Racing wheel", "kind": "racing_wheel"},
        {"id": "light_gun", "name": "Light gun", "kind": "light_gun"},
        {"id": "paddle", "name": "Paddle", "kind": "paddle"},
        {"id": "trackball", "name": "Trackball", "kind": "trackball"},
        {"id": "spinner", "name": "Spinner", "kind": "spinner"},
        {"id": "keyboard", "name": "Keyboard", "kind": "keyboard"},
        {"id": "mouse", "name": "Mouse", "kind": "mouse"},
        {"id": "vr", "name": "VR", "kind": "vr"},
    ]
    value = merged.get("controller_types", default)
    if value is None or (isinstance(value, list) and not value):
        return default
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError("Controller types must be a list of at most 40 entries.")
    allowed = {item["kind"] for item in default}
    clean = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Controller types must be objects.")
        entry_id = str(item.get("id", "")).strip()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", entry_id) or entry_id in seen:
            raise ValueError("Controller type ids must be unique lowercase identifiers.")
        seen.add(entry_id)
        name = str(item.get("name", "")).strip()[:60]
        kind = str(item.get("kind", entry_id)).strip()
        if not name or kind not in allowed:
            raise ValueError("Controller type name/kind is invalid.")
        clean.append({"id": entry_id, "name": name, "kind": kind})
    return clean


def _clean_emulator_platform_settings(merged):
    value = merged.get("emulator_platform_settings", {})
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 500:
        raise ValueError("Platform emulator settings must be an object.")
    from pkg.parity.parity_emulator_defs import valid_core_name
    clean = {}
    for platform, entry in value.items():
        platform_name = str(platform or "").strip()[:120]
        if not platform_name or not isinstance(entry, dict):
            continue
        adapter_id = str(entry.get("adapter_id", "")).strip()
        core = str(entry.get("retroarch_core", "")).strip()
        extra = entry.get("extra_args", "")
        if isinstance(extra, list):
            extra = " ".join(str(item) for item in extra)
        extra = str(extra or "").strip()[:2000]
        if adapter_id and len(adapter_id) > 120:
            continue
        if core and not valid_core_name(core):
            raise ValueError(f"Invalid RetroArch core for {platform_name}.")
        clean[platform_name] = {
            "adapter_id": adapter_id,
            "retroarch_core": core,
            "extra_args": extra,
        }
    return clean


def _clean_controller_map(merged):
    mapping = merged.get("controller_map", {})
    if not isinstance(mapping, dict):
        raise ValueError("Controller mapping must be an object.")
    clean_mapping = {}
    for action, button in mapping.items():
        if action not in CONTROLLER_ACTIONS:
            raise ValueError("Controller button mappings must use buttons 0 through 31.")
        if button in (None, ""):
            continue
        if isinstance(button, bool) or not isinstance(button, int) or not 0 <= button <= 31:
            raise ValueError("Controller button mappings must use buttons 0 through 31.")
        clean_mapping[action] = button
    by_button = {}
    for action, button in clean_mapping.items():
        if button in by_button:
            raise ValueError(f"Button {button} is assigned to both {by_button[button]} and {action}.")
        by_button[button] = action
    return clean_mapping


def _clean_keyboard_map(merged):
    mapping = merged.get("keyboard_map", {})
    if not isinstance(mapping, dict):
        raise ValueError("Keyboard mapping must be an object.")
    clean = {}
    for action, spec in mapping.items():
        if action not in KEYBOARD_ACTIONS:
            continue
        if spec in (None, ""):
            continue
        text = str(spec).strip()
        if len(text) > 40 or not KEY_SPEC_RE.fullmatch(text):
            raise ValueError(f"Keyboard shortcut for {action} is not a supported key.")
        clean[action] = text
    seen = {}
    for action, spec in clean.items():
        key = spec.casefold()
        if key in seen:
            raise ValueError(f"Keyboard shortcut {spec} is assigned to both {seen[key]} and {action}.")
        seen[key] = action
    return clean


def _clean_float_bounded(merged, key, default, low, high, message):
    value = merged.get(key, default)
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise ValueError(message)
    number = _safe_float(value, default, message)
    if not low <= number <= high:
        raise ValueError(message)
    return number


def _clean_bigbox_hidden_lists(merged):
    return (
        _clean_str_list(merged, "bigbox_hidden_platforms", 500, 200),
        _clean_str_list(merged, "bigbox_hidden_categories", 500, 200),
        _clean_str_list(merged, "bigbox_hidden_playlists", 500, 200),
    )


def _clean_controller_prompt_hint(merged):
    value = merged.get("controller_prompt_hint", "")
    if isinstance(value, bool):
        return "A Play · B Back · M Menu" if value else ""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("controller_prompt_hint must be a string.")
    return value.strip()[:200]


def _clean_mood(merged):
    enabled = bool(merged.get("mood_match_enabled", False))
    bigbox = bool(merged.get("mood_match_bigbox", False))
    # Big Box mood only matters when desktop mood is on.
    return enabled, enabled and bigbox


def _clean_party(merged):
    queue = merged.get("party_queue", [])
    if queue is None:
        queue = []
    if not isinstance(queue, list):
        raise ValueError("Party queue must be a list of game ids.")
    clean_queue = []
    for item in queue:
        game_id = str(item or "").strip()
        if game_id and game_id not in clean_queue:
            clean_queue.append(game_id)
    clean_queue = clean_queue[:50]
    players = merged.get("party_players", 2)
    if players is None:
        players = 2
    try:
        players = int(players)
    except (TypeError, ValueError) as error:
        raise ValueError("Party players must be an integer.") from error
    if not 2 <= players <= 8:
        raise ValueError("Party players must be between 2 and 8.")
    index = merged.get("party_index", 0)
    if index is None:
        index = 0
    try:
        index = int(index)
    except (TypeError, ValueError) as error:
        raise ValueError("Party index must be an integer.") from error
    if index < 0:
        raise ValueError("Party index must be >= 0.")
    if clean_queue:
        index = min(index, len(clean_queue) - 1)
    else:
        index = 0
    return clean_queue, players, index


def _clean_cloud_folder(merged):
    cloud_folder = str(merged.get("cloud_folder", "")).strip()
    if cloud_folder:
        cloud_path = Path(cloud_folder).expanduser()
        if not cloud_path.is_absolute() or not cloud_path.is_dir():
            raise ValueError(f"Cloud sync folder does not exist: {cloud_path}")
        cloud_folder = str(cloud_path)
    return cloud_folder


def _clean_commands_group(merged):
    return (
        clean_commands(merged.get("startup_commands", [])),
        clean_commands(merged.get("shutdown_commands", [])),
    )


def _clean_progress_automation(merged):
    track_session_history = bool(merged.get("track_session_history", True))
    backup_on_close = bool(merged.get("backup_on_close", False))
    progress_automation_enabled = bool(merged.get("progress_automation_enabled", False))
    play_minutes = _safe_int(merged.get("progress_automation_play_minutes", 30), 30,
                             "Progress automation play minutes must be between 0 and 100000.")
    idle_days = _safe_int(merged.get("progress_automation_idle_days", 30), 30,
                          "Progress automation idle days must be between 0 and 3650.")
    if not 0 <= play_minutes <= 100000:
        raise ValueError("Progress automation play minutes must be between 0 and 100000.")
    if not 0 <= idle_days <= 3650:
        raise ValueError("Progress automation idle days must be between 0 and 3650.")
    welcome_completed = bool(merged.get("welcome_completed", False))
    return (
        track_session_history,
        backup_on_close,
        progress_automation_enabled,
        play_minutes,
        idle_days,
        welcome_completed,
    )


def _clean_image_group(merged):
    image_group = str(merged.get("image_group", "cover"))
    if image_group not in {"cover", "background", "screenshot", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual"}:
        raise ValueError("Unknown default image group.")
    return image_group


COVER_MODELS = ("box", "jewel", "long_jewel", "double_jewel", "cassette", "none")
MODEL_BACKS = ("straight", "angled")
MODEL_CASES = ("clear", "solid")
MODEL_ROTATION_LOCKS = ("free", "horizontal", "vertical")


def _clean_cover_models(merged):
    model = _clean_enum(merged, "cover_model", COVER_MODELS, "box")
    by_platform = _clean_dict(merged, "cover_model_by_platform", 200)
    for platform, value in list(by_platform.items()):
        if not isinstance(value, str) or value not in COVER_MODELS:
            raise ValueError(f"Unknown cover model for {platform}.")
    rotation_lock = _clean_enum(merged, "model_rotation_lock", MODEL_ROTATION_LOCKS, "free")
    back_by_platform = _clean_dict(merged, "model_back_by_platform", 200)
    for platform, value in list(back_by_platform.items()):
        if not isinstance(value, str) or value not in MODEL_BACKS:
            raise ValueError(f"Unknown cassette back style for {platform}.")
    case_by_platform = _clean_dict(merged, "model_case_by_platform", 200)
    for platform, value in list(case_by_platform.items()):
        if not isinstance(value, str) or value not in MODEL_CASES:
            raise ValueError(f"Unknown cassette case style for {platform}.")
    rotation_by_platform = _clean_dict(merged, "model_rotation_lock_by_platform", 200)
    for platform, value in list(rotation_by_platform.items()):
        if not isinstance(value, str) or value not in MODEL_ROTATION_LOCKS:
            raise ValueError(f"Unknown rotation lock for {platform}.")
    return model, by_platform, rotation_lock, back_by_platform, case_by_platform, rotation_by_platform


def _clean_reader(merged):
    theme = _clean_enum(merged, "reader_theme", {"light", "dark"}, "light")
    spread = _clean_enum(merged, "reader_spread", {"single", "spread"}, "single")
    return theme, spread


def _clean_video_settings(merged):
    controls = _clean_bool(merged, "bigbox_video_controls", True)
    volume = _clean_float_bounded(merged, "bigbox_video_volume", 0.8, 0.0, 1.0,
                                  "Big Box video volume must be between 0 and 1.")
    backdrop = _clean_bool(merged, "bigbox_video_backdrop", False)
    loop = _clean_bool(merged, "bigbox_startup_video_loop", False)
    videos = _clean_dict(merged, "platform_videos", 200)
    for platform, path in list(videos.items()):
        text = str(path or "").strip()
        if not text:
            del videos[platform]
            continue
        candidate = Path(text).expanduser()
        if not candidate.is_absolute() or not candidate.is_file():
            raise ValueError(f"Platform video does not exist: {text}")
        videos[platform] = str(candidate)
    return controls, volume, backdrop, loop, videos


def _clean_badge_visibility(merged):
    badge_visibility = merged.get("badge_visibility", ["favorite", "installed", "saves", "documents", "progress", "storefront", "achievements", "rating"])
    allowed_badges = {"favorite", "installed", "missing_media", "saves", "documents", "versions", "storefront", "achievements", "highscores", "progress", "rating", "broken", "portable", "controller"}
    if not isinstance(badge_visibility, list) or not set(badge_visibility) <= allowed_badges:
        raise ValueError("Badge visibility must contain known badge names.")
    return badge_visibility


def _clean_limits(merged):
    save_backup_limit = _safe_int(merged.get("save_backup_limit", 10), 10,
                                  "Save backup limit must be between 0 and 500.")
    if not 0 <= save_backup_limit <= 500:
        raise ValueError("Save backup limit must be between 0 and 500.")
    media_download_limit = _safe_int(merged.get("media_download_limit", 0), 0,
                                     "Media download limit must be between 0 and 10000.")
    if media_download_limit < 0 or media_download_limit > 10000:
        raise ValueError("Media download limit must be between 0 and 10000.")
    return save_backup_limit, media_download_limit


def _clean_backup_auto(merged):
    """Weekly library auto-backup toggle + retention (1–52)."""
    enabled = bool(merged.get("backup_auto_enabled", False))
    keep = _safe_int(merged.get("backup_auto_keep", AUTO_BACKUP_KEEP), AUTO_BACKUP_KEEP,
                     "Automatic backups kept must be between 1 and 52.")
    if not 1 <= keep <= 52:
        raise ValueError("Automatic backups kept must be between 1 and 52.")
    return enabled, keep


def normalize_health_rescan(value):
    """Library health rescan cadence: daily / weekly / on_startup / off (default weekly)."""
    from pkg.parity.parity_library_health import normalize_rescan_setting
    return normalize_rescan_setting(value)


def _clean_media_types(merged):
    auto_import_media_types = merged.get("auto_import_media_types", [])
    if not isinstance(auto_import_media_types, list) or not set(auto_import_media_types) <= MEDIA_TYPES_ALL:
        raise ValueError("Auto-import media types include an unknown media type.")
    return auto_import_media_types


def _clean_media_queue_policy(merged):
    """P8: per-platform/kind cap plus metadata provider ordering."""
    per_group = _safe_int(merged.get("media_download_limit_per_group", 0), 0,
                          "Per-group media download limit must be between 0 and 100.")
    if per_group < 0 or per_group > 100:
        raise ValueError("Per-group media download limit must be between 0 and 100.")
    priority = _clean_str_list(merged, "metadata_provider_priority", 20, 80)
    from pkg.parity.parity_metadata_providers import BUILTIN_PROVIDER_ORDER
    known = set(BUILTIN_PROVIDER_ORDER)
    priority = [item for item in priority if item in known or item.startswith("plugin:")]
    plugin_priority = _clean_bool(merged, "plugin_metadata_priority", False)
    return per_group, priority, plugin_priority


def _clean_state_retention(merged):
    """T1: resume states kept per game — bounded to the on-disk archive cap."""
    retention = _safe_int(merged.get("state_retention", 1) or 1, 1,
                          "Resume state retention must be between 1 and 20.")
    if not 1 <= retention <= 20:
        raise ValueError("Resume state retention must be between 1 and 20.")
    return retention


def _clean_memory_roots(merged):
    """S5: user-configured memory roots — absolute existing dirs only."""
    roots = merged.get("memories_import_roots", [])
    if roots in (None, ""):
        return []
    if not isinstance(roots, list) or len(roots) > 32:
        raise ValueError("Memory roots must be a list of at most 32 paths.")
    clean = []
    for value in roots:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"Memory root does not exist: {path}")
        if str(path) not in clean:
            clean.append(str(path))
    return clean


def _clean_priority_lists(merged):
    region_priority = merged.get("region_priority", list(REGION_PRIORITY_DEFAULT))
    if not isinstance(region_priority, list) or not region_priority:
        raise ValueError("Region priority must be a non-empty list.")
    video_priority = merged.get("video_priority", ["video_snap", "video_theme", "video_trailer", "video_recording"])
    if not isinstance(video_priority, list) or not set(video_priority) <= set(["video_snap", "video_theme", "video_trailer", "video_recording", "video"]):
        raise ValueError("Invalid video priority list.")
    return region_priority, video_priority


def _clean_library_music(merged):
    library_music = str(merged.get("library_music", "")).strip()
    if library_music and not Path(library_music).expanduser().is_file():
        raise ValueError("Library music path must point to an existing audio file.")
    return library_music


def _clean_currency(merged):
    return clean_currency(merged.get("currency", "USD"))


def _clean_font_family(merged):
    return _clean_enum(merged, "font_family", {"system", "serif", "mono", "humanist"}, "system")


def _clean_bigbox(merged):
    bigbox_mode = str(merged.get("bigbox_mode", "stage"))
    if bigbox_mode not in {"stage", "hybrid", "coverflow"}:
        raise ValueError("Big Box mode must be stage, hybrid, or coverflow.")
    return bigbox_mode


def _clean_storefront(merged):
    storefront_auto_import = merged.get("storefront_auto_import", {})
    if not isinstance(storefront_auto_import, dict):
        raise ValueError("Storefront auto-import settings must be an object.")
    if len(storefront_auto_import) > 20:
        raise ValueError("Storefront auto-import settings may list at most 20 stores.")
    clean = {}
    for key, raw in storefront_auto_import.items():
        key = str(key).strip().casefold()
        if not key or len(key) > 80 or (key not in {"steam", "heroic", "lutris", "gameyfin", "gog", "ea", "ubisoft"} and not key.startswith("plugin:")):
            continue
        if isinstance(raw, bool):
            clean[key] = {"enabled": raw, "platform": "", "include_uninstalled": False}
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"Storefront setting for {key} must be an object or boolean.")
        platform = str(raw.get("platform") or "").strip()
        if len(platform) > 80:
            raise ValueError(f"Storefront platform for {key} must be 80 characters or fewer.")
        clean[key] = {
            "enabled": bool(raw.get("enabled")),
            "platform": platform,
            "include_uninstalled": bool(raw.get("include_uninstalled")),
        }
    return clean


def _clean_obs(merged):
    obs_auto_attach = bool(merged.get("obs_auto_attach", True))
    obs_recording_path = str(merged.get("obs_recording_path", "")).strip()
    if obs_recording_path:
        recording_path = Path(obs_recording_path).expanduser()
        if not recording_path.is_absolute() or not recording_path.is_dir():
            raise ValueError(f"OBS recording folder does not exist: {recording_path}")
        obs_recording_path = str(recording_path)
    return obs_auto_attach, obs_recording_path


def _clean_gameyfin(merged):
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
    return gameyfin_url, gameyfin_install_dir


def _clean_ludusavi(merged):
    ludusavi_backup_path = str(merged.get("ludusavi_backup_path", "")).strip()
    if ludusavi_backup_path:
        backup_path = Path(ludusavi_backup_path).expanduser()
        backup_path.mkdir(parents=True, exist_ok=True)
        ludusavi_backup_path = str(backup_path)
    return ludusavi_backup_path


def _clean_hidden_sections(merged):
    hidden_sidebar_sections = merged.get("hidden_sidebar_sections", [])
    if not isinstance(hidden_sidebar_sections, list):
        raise ValueError("Hidden sidebar sections must be a list.")
    cleaned = []
    for item in hidden_sidebar_sections:
        section = str(item).strip()
        if section not in SIDEBAR_SECTION_IDS or section in cleaned:
            raise ValueError("Hidden sidebar sections must be known, unique section ids.")
        cleaned.append(section)
    return cleaned


def _clean_tracking(merged):
    tracking_mode = str(merged.get("tracking_mode", "default")).strip().casefold()
    if tracking_mode not in TRACKING_MODES:
        raise ValueError("Unknown tracking mode.")
    tracking_delay = _safe_int(merged.get("tracking_delay", 0), 0,
                               "Tracking delay must be between 0 and 600 seconds.")
    tracking_frequency = _safe_float(merged.get("tracking_frequency", 2), 2,
                                     "Tracking frequency must be between 0.5 and 60 seconds.")
    if tracking_delay < 0 or tracking_delay > 600:
        raise ValueError("Tracking delay must be between 0 and 600 seconds.")
    if not 0.5 <= tracking_frequency <= 60:
        raise ValueError("Tracking frequency must be between 0.5 and 60 seconds.")
    return tracking_mode, tracking_delay, tracking_frequency


def _clean_progress_first_play(merged):
    progress_on_first_play = str(merged.get("progress_on_first_play", "Playing")).strip()
    if progress_on_first_play and progress_on_first_play not in PROGRESS:
        raise ValueError("Unknown progress value for first play.")
    return progress_on_first_play


def _clean_progress_suggest(merged):
    # F4a: kill switch for the one-time "Playing?" auto-suggest; on by default.
    return bool(merged.get("backlog_progress_suggest", True))


def _clean_apply_perf(merged):
    apply_perf = str(merged.get("apply_perf", "auto")).strip().casefold()
    if apply_perf not in {"off", "auto", "always"}:
        raise ValueError("Apply performance limits must be off, auto, or always.")
    return apply_perf


def _clean_password(merged):
    return str(merged.get("gameyfin_password", "")).strip()


def _preset_opt_int(preset, name, field, maximum):
    """Parse an optional non-negative int for a custom gamescope preset."""
    try:
        value = int(preset.get(field) or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {field} for gamescope preset {name}.") from error
    if value < 0 or value > maximum:
        raise ValueError(f"{field} for gamescope preset {name} must be between 0 and {maximum}.")
    return value


def _clean_gamescope_presets(merged):
    """Validate settings.gamescope_custom_presets (1.8.0, ADR 0016 follow-up)."""
    presets = merged.get("gamescope_custom_presets", [])
    if not isinstance(presets, list):
        raise ValueError("Custom gamescope presets must be a list.")
    if len(presets) > 16:
        raise ValueError("At most 16 custom gamescope presets are allowed.")
    clean = []
    seen = set()
    for preset in presets:
        if not isinstance(preset, dict):
            raise ValueError("Custom gamescope presets must be objects.")
        name = str(preset.get("name", "")).strip()
        if not name:
            raise ValueError("Custom gamescope presets need a name.")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate gamescope preset name: {name}")
        seen.add(key)
        entry = {"name": name[:30]}
        width = _preset_opt_int(preset, name, "width", 7680)
        height = _preset_opt_int(preset, name, "height", 4320)
        if width and height:
            entry["width"], entry["height"] = width, height
            inner_w = _preset_opt_int(preset, name, "inner_width", 7680)
            inner_h = _preset_opt_int(preset, name, "inner_height", 4320)
            if inner_w and inner_h:
                entry["inner_width"], entry["inner_height"] = inner_w, inner_h
        refresh = _preset_opt_int(preset, name, "refresh", 1000)
        if refresh:
            entry["refresh"] = refresh
        for flag in ("integer", "stretch", "fsr", "borderless"):
            if preset.get(flag):
                entry[flag] = True
        sharpness = _preset_opt_int(preset, name, "fsr_sharpness", 5)
        if entry.get("fsr"):
            entry["fsr_sharpness"] = sharpness or 2
        extra = preset.get("extra_args")
        if isinstance(extra, str) and extra.strip():
            entry["extra_args"] = extra.strip().split()[:20]
        elif isinstance(extra, list) and extra:
            entry["extra_args"] = [str(item) for item in extra if str(item).strip()][:20]
        clean.append(entry)
    return clean


def clean_settings(merged):
    clean_folders = _clean_watch_folders(merged)
    seconds = _clean_screensaver_seconds(merged)
    clean_mapping = _clean_controller_map(merged)
    controller_types = _clean_controller_types(merged)
    integration_tokens = clean_tokens(merged.get("integration_tokens", {}))
    emulator_platform_settings = _clean_emulator_platform_settings(merged)
    cloud_folder = _clean_cloud_folder(merged)
    startup_commands, shutdown_commands = _clean_commands_group(merged)
    (track_session_history, backup_on_close, progress_automation_enabled, play_minutes, idle_days, welcome_completed) = _clean_progress_automation(merged)
    image_group = _clean_image_group(merged)
    badge_visibility = _clean_badge_visibility(merged)
    save_backup_limit, media_download_limit = _clean_limits(merged)
    backup_auto_enabled, backup_auto_keep = _clean_backup_auto(merged)
    save_management_enabled = _clean_bool(merged, "save_management_enabled", True)
    auto_import_media_types = _clean_media_types(merged)
    media_download_limit_per_group, metadata_provider_priority, plugin_metadata_priority = _clean_media_queue_policy(merged)
    memories_import_roots = _clean_memory_roots(merged)
    state_retention = _clean_state_retention(merged)
    region_priority, video_priority = _clean_priority_lists(merged)
    library_music = _clean_library_music(merged)
    currency = _clean_currency(merged)
    font_family = _clean_font_family(merged)
    bigbox_mode = _clean_bigbox(merged)
    clean_storefront = _clean_storefront(merged)
    obs_auto_attach, obs_recording_path = _clean_obs(merged)
    gameyfin_url, gameyfin_install_dir = _clean_gameyfin(merged)
    ludusavi_backup_path = _clean_ludusavi(merged)
    hidden_sidebar_sections = _clean_hidden_sections(merged)
    tracking_mode, tracking_delay, tracking_frequency = _clean_tracking(merged)
    progress_on_first_play = _clean_progress_first_play(merged)
    backlog_progress_suggest = _clean_progress_suggest(merged)
    apply_perf = _clean_apply_perf(merged)
    gameyfin_password = _clean_password(merged)
    controller_prompt_hint = _clean_controller_prompt_hint(merged)
    mood_match_enabled, mood_match_bigbox = _clean_mood(merged)
    party_queue, party_players, party_index = _clean_party(merged)
    sidebar_sections = _clean_sidebar_sections(merged)
    progress_values = _clean_progress_values(merged)
    keyboard_map = _clean_keyboard_map(merged)
    pointer_nav_enabled = _clean_bool(merged, "pointer_nav_enabled", True)
    mouse_wheel_nav = _clean_bool(merged, "mouse_wheel_nav", True)
    spinner_sensitivity = max(1, min(20, _safe_int(
        merged.get("spinner_sensitivity", 6), 6,
        "Spinner sensitivity must be a number between 1 and 20.")))
    bigbox_wheel_speed = _clean_float_bounded(merged, "bigbox_wheel_speed", 1.0, 0.25, 3.0,
                                               "Big Box wheel speed must be between 0.25 and 3.0.")
    bigbox_back_hold_seconds = _clean_float_bounded(merged, "bigbox_back_hold_seconds", 0.8, 0.0, 2.0,
                                                     "Big Box back hold must be between 0 and 2 seconds.")
    battery_indicator = _clean_bool(merged, "battery_indicator", True)
    hidden_platforms, hidden_categories, hidden_playlists = _clean_bigbox_hidden_lists(merged)
    reader_theme, reader_spread = _clean_reader(merged)
    (cover_model, cover_model_by_platform, model_rotation_lock,
     model_back_by_platform, model_case_by_platform, model_rotation_lock_by_platform) = _clean_cover_models(merged)
    bigbox_video_controls, bigbox_video_volume, bigbox_video_backdrop, bigbox_startup_video_loop, platform_videos = _clean_video_settings(merged)
    bigbox_sounds_enabled = _clean_bool(merged, "bigbox_sounds_enabled", True)
    auto_import_excluded_platforms = _clean_str_list(merged, "auto_import_excluded_platforms", 100, 80)
    emulator_update_checks = _clean_str_list(merged, "emulator_update_checks", 100, 100)
    path_mode = str(merged.get("path_mode") or "absolute")
    if path_mode not in PATH_MODES:
        raise ValueError("Library path mode must be absolute or portable.")
    try:
        library_root = normalize_root(str(merged.get("library_root") or ""), windows=IS_WINDOWS)
    except PathTokenError as error:
        raise ValueError(str(error)) from error
    if library_root and not Path(library_root).is_dir():
        raise ValueError(f"Library root folder does not exist: {library_root}")
    media_manifest_enabled = _clean_bool(merged, "media_manifest_enabled", True)
    return {
            "watch_folders": clean_folders,
            "screensaver_seconds": seconds,
            "controller_map": clean_mapping,
            "controller_types": controller_types,
            "integration_tokens": integration_tokens,
            "emulator_platform_settings": emulator_platform_settings,
            "badge_visibility": [str(item) for item in badge_visibility],
            "cloud_folder": cloud_folder,
            "library_sync_enabled": bool(merged.get("library_sync_enabled", False)),
            "startup_commands": startup_commands,
            "shutdown_commands": shutdown_commands,
            "track_session_history": track_session_history,
            "session_recap_enabled": bool(merged.get("session_recap_enabled", True)),
            "quick_resume_enabled": bool(merged.get("quick_resume_enabled", True)),
            "moments_autocapture": bool(merged.get("moments_autocapture", True)),
            "ra_hardcore": _clean_bool(merged, "ra_hardcore", False),
            "state_retention": state_retention,
            "backup_on_close": backup_on_close,
            "backup_auto_enabled": backup_auto_enabled,
            "backup_auto_keep": backup_auto_keep,
            "save_backup_limit": save_backup_limit,
            "save_management_enabled": save_management_enabled,
            "progress_automation_enabled": progress_automation_enabled,
            "progress_automation_play_minutes": play_minutes,
            "progress_automation_idle_days": idle_days,
            "welcome_completed": welcome_completed,
            "image_group": image_group,
            "auto_import_media_types": sorted(set(auto_import_media_types) or {"cover", "background", "screenshots"}),
            "media_download_limit": media_download_limit,
            "media_download_limit_per_group": media_download_limit_per_group,
            "metadata_provider_priority": metadata_provider_priority,
            "plugin_metadata_priority": plugin_metadata_priority,
            "memories_import_enabled": bool(merged.get("memories_import_enabled", False)),
            "memories_import_roots": memories_import_roots,
            "steamgrid_enabled": bool(merged.get("steamgrid_enabled", True)),
            "region_priority": [str(item) for item in region_priority],
            "video_priority": [str(item) for item in video_priority],
            "library_music": library_music,
            "video_bgm_mix": bool(merged.get("video_bgm_mix", False)),
            "bigbox_mode": bigbox_mode,
            "bigbox_start_at_launch": bool(merged.get("bigbox_start_at_launch", False)),
            "show_playlist_actions": bool(merged.get("show_playlist_actions", True)),
            "show_insights": bool(merged.get("show_insights", True)),
            "show_launch_badges": bool(merged.get("show_launch_badges", True)),
            "hidden_sidebar_sections": [str(item) for item in hidden_sidebar_sections][:20],
            "storefront_auto_import": clean_storefront,
            "auto_import_excluded_platforms": auto_import_excluded_platforms,
            "emulator_update_checks": emulator_update_checks,
            "path_mode": path_mode,
            "library_root": library_root,
            "media_manifest_enabled": media_manifest_enabled,
            "obs_auto_attach": obs_auto_attach,
            "obs_recording_path": obs_recording_path,
            "obs_replay_enabled": bool(merged.get("obs_replay_enabled", False)),
            "obs_websocket_url": str(merged.get("obs_websocket_url", "")).strip()[:256],
            "obs_websocket_password": str(merged.get("obs_websocket_password", ""))[:256],
            "obs_websocket_timeout": max(0.1, min(30.0, _safe_float(merged.get("obs_websocket_timeout", 5.0), 5.0,
                                                                             "OBS WebSocket timeout must be between 0.1 and 30 seconds."))),
            "dynamic_play_button": bool(merged.get("dynamic_play_button", True)),
            "custom_field_defs": custom_field_defs({"custom_field_defs": merged.get("custom_field_defs", [])}),
            "platform_categories": platform_categories({"platform_categories": merged.get("platform_categories", {})}),
            "list_columns": [str(item) for item in merged.get("list_columns", list(LIST_COLUMNS_DEFAULT))][:12],
            "list_sort": str(merged.get("list_sort", "title")).strip()[:20],
            "list_sort_dir": str(merged.get("list_sort_dir", "")).strip()[:10],
            "library_view": str(merged.get("library_view", "grid")),
            "cover_grouping": str(merged.get("cover_grouping", "shape")),
            "locale": str(merged.get("locale", "en"))[:5],
            "attract_mode_seconds": _safe_int(merged.get("attract_mode_seconds", seconds or 90), seconds or 90,
                                               "Attract mode delay must be between 0 and 3600 seconds."),
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
            "tracking_process_name": str(merged.get("tracking_process_name", "")).strip()[:100],
            "sidebar_sections": sidebar_sections,
            "progress_values": progress_values,
            "keyboard_map": keyboard_map,
            "pointer_nav_enabled": pointer_nav_enabled,
            "mouse_wheel_nav": mouse_wheel_nav,
            "spinner_sensitivity": spinner_sensitivity,
            "bigbox_wheel_speed": bigbox_wheel_speed,
            "bigbox_back_hold_seconds": bigbox_back_hold_seconds,
            "battery_indicator": battery_indicator,
            "bigbox_hidden_platforms": hidden_platforms,
            "bigbox_hidden_categories": hidden_categories,
            "bigbox_hidden_playlists": hidden_playlists,
            "reader_theme": reader_theme,
            "reader_spread": reader_spread,
            "cover_model": cover_model,
            "cover_model_by_platform": cover_model_by_platform,
            "model_rotation_lock": model_rotation_lock,
            "model_back_by_platform": model_back_by_platform,
            "model_case_by_platform": model_case_by_platform,
            "model_rotation_lock_by_platform": model_rotation_lock_by_platform,
            "bigbox_video_controls": bigbox_video_controls,
            "bigbox_video_volume": bigbox_video_volume,
            "bigbox_video_backdrop": bigbox_video_backdrop,
            "bigbox_startup_video_loop": bigbox_startup_video_loop,
            "platform_videos": platform_videos,
            "bigbox_sounds_enabled": bigbox_sounds_enabled,
            "controller_prompt_hint": controller_prompt_hint,
            "controller_prompt_pack": str(merged.get("controller_prompt_pack", "xbox")).strip()[:20],
            "apply_perf": apply_perf,
            "progress_on_first_play": progress_on_first_play,
            "backlog_progress_suggest": backlog_progress_suggest,
            "auto_close_store_clients": bool(merged.get("auto_close_store_clients", False)),
            "gamescope_preset": str(merged.get("gamescope_preset", "")).strip()[:30],
            "gamescope_custom_presets": _clean_gamescope_presets(merged),
            "mangohud_enabled": bool(merged.get("mangohud_enabled", False)),
            "mood_match_enabled": mood_match_enabled,
            "mood_match_bigbox": mood_match_bigbox,
            "party_queue": party_queue,
            "party_players": party_players,
            "party_index": party_index,
            "currency": currency,
            "font_family": font_family,
            "household_stats_sharing": bool(merged.get("household_stats_sharing", False)),
            "museum_kiosk_enabled": bool(merged.get("museum_kiosk_enabled", False)),
            "museum_kiosk_pin_hash": str(merged.get("museum_kiosk_pin_hash", ""))[:512],
            "health_rescan": normalize_health_rescan(merged.get("health_rescan", "weekly")),
    }


class SettingsHandlers:
    @route("GET", "/api/profiles")
    def _api_get_api_profiles(self, parsed):
        state = load_state_view()
        self.send_json(200, {"profiles": state["profiles"], "detected": discover_profiles()})
        return

    @route("GET", "/api/perf_profiles")
    def _api_get_api_perf_profiles(self, parsed):
        self.send_json(200, {"perf_profiles": load_state_view().get("perf_profiles", {})})
        return

    @route("GET", "/api/settings")
    def _api_get_api_settings(self, parsed):
        self.send_json(200, public_settings())
        return

    @route("GET", "/api/ra/settings")
    def _api_get_api_ra_settings(self, parsed):
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
            from api_errors import BadRequest
            raise BadRequest(str(error)) from None
        return

    @route("POST", "/api/profiles")
    def _api_post_api_profiles(self, payload):
        self.save_profiles(payload)

    @route("POST", "/api/perf_profiles")
    def _api_post_api_perf_profiles(self, payload):
        self.save_perf_profiles(payload)

    @route("POST", "/api/settings")
    def _api_post_api_settings(self, payload):
        self.save_settings(payload)

    @route("GET", "/api/v2/integrations/steam")
    def _api_get_api_v2_integrations_steam(self, parsed):
        self.send_json(200, steam_credential_status(data_dir=DATA.parent))
        return

    @route("POST", "/api/v2/integrations/steam")
    def _api_post_api_v2_integrations_steam(self, payload):
        status = save_steam_credentials(
            DATA.parent,
            api_key=payload.get("api_key") if "api_key" in payload else None,
            steam_id=payload.get("steam_id") if "steam_id" in payload else None,
        )
        self.send_json(200, status)
        return

    @route("POST", "/api/v2/integrations/steam/test")
    def _api_post_api_v2_integrations_steam_test(self, payload):
        credentials = load_steam_credentials(DATA.parent)
        if not credentials.get("api_key") or not credentials.get("steam_id"):
            raise ValueError("Save a Steam API key and Steam ID first.")
        owned = fetch_owned_games(credentials["steam_id"], api_key=credentials["api_key"], data_dir=DATA.parent)
        self.send_json(200, {"ok": True, "games": len(owned)})
        return

    @route("POST", "/api/v2/integrations/steam/refresh")
    def _api_post_api_v2_integrations_steam_refresh(self, payload):
        credentials = load_steam_credentials(DATA.parent)
        if not credentials.get("api_key") or not credentials.get("steam_id"):
            raise ValueError("Save a Steam API key and Steam ID first.")

        def worker(_cancel_event):
            owned = fetch_owned_games(credentials["steam_id"], api_key=credentials["api_key"], data_dir=DATA.parent)
            updated = [0]

            def mutate(state):
                indexed = {str(game.get("game_id")): game for game in state.get("games", []) if isinstance(game, dict)}
                merged = merge_steam_stats(list(indexed.values()), owned)
                updated[0] = sum(1 for game in merged if game.get("steam_app_id"))
                return {"games": updated[0], "owned": len(owned)}

            _, result = transact_state(mutate)
            return result

        job = JOB_MANAGER.submit("steam.stats", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})
        return

    @route("POST", "/api/ra/inject")
    def _api_post_api_ra_inject(self, payload):
        self.inject_ra()

    @route("POST", "/api/ra/settings")
    def _api_post_api_ra_settings(self, payload):
        self.save_ra_settings(payload)

    @route("POST", "/api/ra/game")
    def _api_post_api_ra_game(self, payload):
        self.ra_game(payload)

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

    def save_settings(self, payload):
        # Hold the local state lock across the snapshot, validation, and commit
        # so a concurrent partial save cannot observe a stale settings base.
        with STATE_LOCK:
            return self._save_settings_locked(payload)

    def _save_settings_locked(self, payload):
        existing_settings = dict(load_state().get("settings", {}))
        merged = dict(existing_settings)
        for key, value in payload.items():
            if key in {"gameyfin_password", "obs_websocket_password"} and not str(value).strip():
                continue
            merged[key] = value
        normalized_settings = clean_settings(merged)
        existing_path_settings = (existing_settings.get("path_mode", "absolute"), existing_settings.get("library_root", ""))
        if (normalized_settings.get("path_mode", "absolute"), normalized_settings.get("library_root", "")) != existing_path_settings:
            # Changing the folder or the path mode rebases stored paths; Move library previews that first (ADR 0090).
            raise ValueError("Use Move library to change the library folder or path mode; it previews the change first.")
        enabling_library_sync = (
            bool(normalized_settings.get("library_sync_enabled"))
            and not bool(existing_settings.get("library_sync_enabled"))
        )
        def mutate(state):
            settings = state.setdefault("settings", {})
            incoming_keys = {
                key for key, value in payload.items()
                if key not in {"gameyfin_password", "obs_websocket_password"} or str(value).strip()
            }
            # Drop keys nobody knows about before they reach the store.
            clean_payload, dropped = sanitize_settings(payload)
            if dropped:
                LOGGER.warning("Dropping unknown settings keys: %s", ", ".join(sorted(map(str, dropped))))
            incoming_keys = {key for key in incoming_keys if key in KNOWN_SETTINGS}
            for key, value in normalized_settings.items():
                # Storefront configs gained per-source options; rewrite legacy
                # booleans even on a partial save so every reader sees one shape.
                if key in incoming_keys or key not in settings or key == "storefront_auto_import":
                    settings[key] = value
            if enabling_library_sync:
                from pkg.parity.parity_library_sync import bootstrap_local_catalog
                bootstrap_local_catalog(state)
        state = update_state_with_result(mutate)[0]
        self.send_json(200, public_settings(state))

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
        cache_path = DATA.parent / "cache" / "retroachievements" / f"{stable_game_id}.json"
        try:
            from backend_io import atomic_write_text

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                cache_path,
                json.dumps({
                    "earned": progress.get("earned", 0),
                    "earned_hardcore": progress.get("earned_hardcore", 0),
                    "total": progress.get("total", 0),
                    "progress_pct": progress.get("progress_pct", 0),
                    "mastered": progress.get("mastered", False),
                    "beaten": progress.get("beaten", 0),
                    "achievements": (progress.get("achievements") or [])[:500],
                }),
                mode=0o600,
            )
        except OSError:
            LOGGER.warning("RetroAchievements cache could not be written", exc_info=True)
        from pkg.state.cache import invalidate_state_cache

        invalidate_state_cache()
        self.send_json(200, progress)

    def inject_ra(self):
        credentials = load_ra_credentials(DATA.parent)
        if not credentials:
            raise ValueError("Configure RetroAchievements first.")
        self.send_json(200, inject_retroachievements(credentials, hardcore=bool(load_state_view().get("settings", {}).get("ra_hardcore", False))))
