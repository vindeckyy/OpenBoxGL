"""SettingsHandlers capability handlers. Settings, profiles, performance profiles, and RetroAchievements.

Method bodies reference DATA, load_state, transact_state, and other
names from the live ``web_app`` namespace. ``rebind_methods`` repoints
each function's ``__globals__`` at that namespace, so the bodies run
verbatim without circular imports or snapshotting process-global state.
"""

from handlers import rebind_methods


class SettingsHandlers:
    def _api_get_api_profiles(self, parsed):
        state = load_state_view()
        self.send_json(200, {"profiles": state["profiles"], "detected": discover_profiles()})
        return

    def _api_get_api_perf_profiles(self, parsed):
        self.send_json(200, {"perf_profiles": load_state_view().get("perf_profiles", {})})
        return

    def _api_get_api_settings(self, parsed):
        self.send_json(200, public_settings())
        return

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
            self.send_json(400, {"error": str(error)})
        return

    def _api_post_api_profiles(self, payload):
        self.save_profiles(payload)

    def _api_post_api_perf_profiles(self, payload):
        self.save_perf_profiles(payload)

    def _api_post_api_settings(self, payload):
        self.save_settings(payload)

    def _api_post_api_ra_inject(self, payload):
        self.inject_ra()

    def _api_post_api_ra_settings(self, payload):
        self.save_ra_settings(payload)

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
                "cover_grouping": str(merged.get("cover_grouping", "shape")),
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

    def inject_ra(self):
        credentials = load_ra_credentials(DATA.parent)
        if not credentials:
            raise ValueError("Configure RetroAchievements first.")
        self.send_json(200, inject_retroachievements(credentials))


rebind_methods(SettingsHandlers)
