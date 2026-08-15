"""Settings key registry: one source of truth for keys in state["settings"].

Unknown keys are dropped on save; when adding a setting, add it here and validate it in _save_settings_locked.
"""

KNOWN_SETTINGS = {
    # watch folders and import
    "watch_folders",
    "storefront_auto_import",
    "emulator_scan_configs",
    "import_exclusions",
    # launch and sessions
    "track_session_history",
    "tracking_mode",
    "tracking_delay",
    "tracking_frequency",
    "tracking_process_name",
    "progress_automation_enabled",
    "progress_automation_play_minutes",
    "progress_automation_idle_days",
    "progress_on_first_play",
    "auto_close_store_clients",
    "apply_perf",
    # library presentation
    "library_view",
    "cover_grouping",
    "list_columns",
    "image_group",
    "image_group_by_platform",
    "image_group_by_playlist",
    "badge_visibility",
    "hidden_sidebar_sections",
    "sidebar_sections",
    "theme",
    "theme_by_platform",
    "platform_categories",
    "platform_documents",
    "custom_field_defs",
    "filter_presets",
    # media and metadata
    "media_download_limit",
    "auto_import_media_types",
    "region_priority",
    "video_priority",
    "library_music",
    "video_bgm_mix",
    "active_media_packs",
    # big box and display
    "bigbox_mode",
    "bigbox_quick",
    "bigbox_startup_video",
    "bigbox_shutdown_commands",
    "attract_mode_seconds",
    "screensaver_seconds",
    "locale",
    # commands
    "startup_commands",
    "shutdown_commands",
    # backup and cloud
    "backup_on_close",
    "save_backup_limit",
    "cloud_folder",
    "last_cloud_sync",
    "last_update_check",
    # integrations
    "obs_auto_attach",
    "obs_recording_path",
    "gameyfin_url",
    "gameyfin_username",
    "gameyfin_password",
    "gameyfin_password_set",
    "gameyfin_provider",
    "gameyfin_install_dir",
    "ludusavi_backup_path",
    # webhooks
    "webhooks",
    "webhook_attempts",
    "webhook_timeout",
    # controller prompts and mapping
    "controller_map",
    "controller_prompt_hint",
    "controller_prompt_pack",
    # window and tray
    "tray_enabled",
    "minimize_to_tray",
    # onboarding
    "welcome_completed",
    # misc UI toggles
    "show_playlist_actions",
    "dynamic_play_button",
    "gamescope_guest",
}


def sanitize_settings(settings):
    """Return a copy of settings containing only known keys; unknown keys are dropped (names returned)."""
    if not isinstance(settings, dict):
        return {}, []
    dropped = [key for key in settings if key not in KNOWN_SETTINGS]
    return {key: value for key, value in settings.items() if key in KNOWN_SETTINGS}, dropped
