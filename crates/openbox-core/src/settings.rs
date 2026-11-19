//! Settings key registry: exact 161-key destructive allowlist matching `settings_schema.py`
//! and ratcheted by `scripts/contracts/settings.json` (ADR 0060).

use serde_json::{Map, Value};

/// Sorted canonical list of all 161 `KNOWN_SETTINGS` keys.
pub const KNOWN_SETTINGS: &[&str] = &[
    "active_media_packs",
    "apply_perf",
    "attract_mode_seconds",
    "auto_close_store_clients",
    "auto_import_excluded_platforms",
    "auto_import_media_types",
    "backlog_progress_suggest",
    "backup_auto_enabled",
    "backup_auto_keep",
    "backup_on_close",
    "badge_visibility",
    "battery_indicator",
    "bigbox_back_hold_seconds",
    "bigbox_hidden_categories",
    "bigbox_hidden_platforms",
    "bigbox_hidden_playlists",
    "bigbox_mode",
    "bigbox_quick",
    "bigbox_shutdown_commands",
    "bigbox_sounds_enabled",
    "bigbox_start_at_launch",
    "bigbox_startup_video",
    "bigbox_startup_video_loop",
    "bigbox_video_backdrop",
    "bigbox_video_controls",
    "bigbox_video_volume",
    "bigbox_wheel_speed",
    "cloud_folder",
    "controller_map",
    "controller_prompt_hint",
    "controller_prompt_pack",
    "controller_types",
    "cover_grouping",
    "cover_model",
    "cover_model_by_platform",
    "currency",
    "custom_field_defs",
    "deals_enabled",
    "deals_min_drop_pct",
    "deals_poll_hours",
    "deals_store_cc",
    "dynamic_play_button",
    "emulator_platform_settings",
    "emulator_scan_configs",
    "emulator_update_checks",
    "filter_presets",
    "font_family",
    "gamescope_custom_presets",
    "gamescope_guest",
    "gamescope_preset",
    "gameyfin_install_dir",
    "gameyfin_password",
    "gameyfin_password_set",
    "gameyfin_provider",
    "gameyfin_url",
    "gameyfin_username",
    "health_rescan",
    "hidden_sidebar_sections",
    "household_stats_sharing",
    "image_group",
    "image_group_by_platform",
    "image_group_by_playlist",
    "import_exclusions",
    "integration_tokens",
    "keyboard_map",
    "last_auto_backup",
    "last_cloud_sync",
    "last_health_rescan",
    "last_update_check",
    "library_journal_enabled",
    "library_music",
    "library_root",
    "library_sync_enabled",
    "library_view",
    "list_columns",
    "list_sort",
    "list_sort_dir",
    "locale",
    "ludusavi_backup_path",
    "mangohud_enabled",
    "media_download_limit",
    "media_download_limit_per_group",
    "media_manifest_enabled",
    "memories_import_enabled",
    "memories_import_roots",
    "metadata_provider_priority",
    "minimize_to_tray",
    "model_back_by_platform",
    "model_case_by_platform",
    "model_rotation_lock",
    "model_rotation_lock_by_platform",
    "moments_autocapture",
    "mood_match_bigbox",
    "mood_match_enabled",
    "mouse_wheel_nav",
    "museum_kiosk_enabled",
    "museum_kiosk_pin_hash",
    "obs_auto_attach",
    "obs_recording_path",
    "obs_replay_enabled",
    "obs_websocket_password",
    "obs_websocket_timeout",
    "obs_websocket_url",
    "party_index",
    "party_players",
    "party_queue",
    "path_mode",
    "platform_categories",
    "platform_documents",
    "platform_videos",
    "plugin_metadata_priority",
    "pointer_nav_enabled",
    "progress_automation_enabled",
    "progress_automation_idle_days",
    "progress_automation_play_minutes",
    "progress_on_first_play",
    "progress_values",
    "quick_resume_enabled",
    "ra_hardcore",
    "reader_spread",
    "reader_theme",
    "region_priority",
    "retroachievements_enabled",
    "save_backup_limit",
    "save_management_enabled",
    "save_vault_auto_days",
    "save_vault_enabled",
    "save_vault_keep",
    "save_vault_on_close",
    "scrape_after_import",
    "scrape_igdb_enabled",
    "scrape_screenscraper_enabled",
    "scrape_steamgrid_enabled",
    "screensaver_seconds",
    "session_recap_enabled",
    "show_insights",
    "show_launch_badges",
    "show_playlist_actions",
    "shutdown_commands",
    "sidebar_sections",
    "spinner_sensitivity",
    "startup_commands",
    "state_retention",
    "steamgrid_enabled",
    "steamgrid_key_configured",
    "storefront_auto_import",
    "theme",
    "theme_by_platform",
    "track_session_history",
    "tracking_delay",
    "tracking_frequency",
    "tracking_mode",
    "tracking_process_name",
    "tray_enabled",
    "video_bgm_mix",
    "video_priority",
    "watch_folders",
    "webhook_attempts",
    "webhook_timeout",
    "webhooks",
    "welcome_completed",
];

#[inline]
pub fn is_known_setting(key: &str) -> bool {
    KNOWN_SETTINGS.binary_search(&key).is_ok()
}

/// Return a copy of `settings` retaining only known keys, plus the list of dropped key names.
pub fn sanitize_settings(settings: &Map<String, Value>) -> (Map<String, Value>, Vec<String>) {
    let mut kept = Map::new();
    let mut dropped = Vec::new();
    for (k, v) in settings {
        if is_known_setting(k) {
            kept.insert(k.clone(), v.clone());
        } else {
            dropped.push(k.clone());
        }
    }
    (kept, dropped)
}

/// Prune unknown keys in-place from a JSON settings map; returns sorted removed key names.
pub fn prune_unknown_settings(settings: &mut Map<String, Value>) -> Vec<String> {
    let mut removed: Vec<String> = settings
        .keys()
        .filter(|k| !is_known_setting(k))
        .cloned()
        .collect();
    removed.sort();
    for key in &removed {
        settings.remove(key);
    }
    removed
}
