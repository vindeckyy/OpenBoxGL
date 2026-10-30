# API reference

Generated from `routes.py`, `contracts.py`, and `routes/registry.py`; do not edit by hand. Regenerate with `python3 scripts/gen_api_docs.py --v2`.

Authentication: requests use the `X-OpenBox-Token` header with the per-process token from `server.token`; the local UI may also use a query token during startup. Public assets and byte/stream endpoints follow their route-specific behavior.

## API v2

The additive surface for 1.13+ features. New work targets `/api/v2/*`; routes listed here may evolve until they are explicitly frozen.

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v2/achievements/override` | `_api_post_api_v2_achievements_override` |
| POST | `/api/v2/achievements/refresh` | `_api_post_api_v2_achievements_refresh` |
| POST | `/api/v2/arcade/kiosk/pin` | `handlers.arcade.kiosk_pin` |
| GET | `/api/v2/arcade/kiosk/status` | `handlers.arcade.kiosk_status` |
| POST | `/api/v2/arcade/kiosk/verify` | `handlers.arcade.kiosk_verify` |
| GET | `/api/v2/backup` | `_api_get_api_backup` |
| POST | `/api/v2/backup/create` | `_api_post_api_backup_create` |
| GET | `/api/v2/backup/diff` | `_api_get_api_v2_backup_diff` |
| GET | `/api/v2/backup/manifest` | `_api_get_api_backup_manifest` |
| POST | `/api/v2/backup/restore` | `_api_post_api_backup_restore` |
| GET | `/api/v2/backups` | `_api_get_api_backups` |
| POST | `/api/v2/bezels/download` | `_api_post_api_bezels_download` |
| POST | `/api/v2/bigbox/mode` | `_api_post_api_bigbox_mode` |
| GET | `/api/v2/bigbox/startup-video` | `_api_get_api_bigbox_startup_video` |
| POST | `/api/v2/bulk/find-replace` | `_api_post_api_v2_bulk_find_replace` |
| GET | `/api/v2/clips` | `handlers.clips.clips_list` |
| POST | `/api/v2/clips/capture` | `handlers.clips.capture_clip` |
| POST | `/api/v2/cloud/sync` | `_api_post_api_cloud_sync` |
| GET | `/api/v2/collections` | `handlers.collections.collections_list` |
| POST | `/api/v2/collections` | `handlers.collections.collections_save` |
| POST | `/api/v2/collections/delete` | `handlers.collections.collections_delete` |
| GET | `/api/v2/compat/report` | `handlers.setup.compat_report` |
| POST | `/api/v2/desktop/install` | `_api_post_api_desktop_install` |
| GET | `/api/v2/diagnostic` | `_api_get_api_diagnostic` |
| GET | `/api/v2/discovery` | `_api_get_api_discovery` |
| GET | `/api/v2/document` | `_api_get_api_document` |
| GET | `/api/v2/emulators` | `_api_get_api_emulators` |
| GET | `/api/v2/emulators/definitions` | `_api_get_api_emulators_definitions` |
| POST | `/api/v2/emulators/defs/rollback` | `handlers.defs._api_post_api_v2_emulators_defs_rollback` |
| GET | `/api/v2/emulators/defs/status` | `handlers.defs._api_get_api_v2_emulators_defs_status` |
| GET | `/api/v2/emulators/defs/update` | `handlers.defs._api_get_api_v2_emulators_defs_update` |
| POST | `/api/v2/emulators/defs/update` | `handlers.defs._api_post_api_v2_emulators_defs_update` |
| GET | `/api/v2/emulators/dependencies` | `_api_get_api_emulators_dependencies` |
| POST | `/api/v2/emulators/install` | `_api_post_api_emulators_install` |
| POST | `/api/v2/emulators/install-all` | `_api_post_api_emulators_install_all` |
| POST | `/api/v2/emulators/open` | `_api_post_api_emulators_open` |
| GET | `/api/v2/emulators/recommend` | `_api_get_api_emulators_recommend` |
| GET | `/api/v2/emulators/registry` | `_api_get_api_v2_emulators_registry` |
| POST | `/api/v2/emulators/scan` | `_api_post_api_emulators_scan` |
| GET | `/api/v2/emulators/scan-configs` | `_api_get_api_emulators_scan_configs` |
| POST | `/api/v2/emulators/scan-configs` | `_api_post_api_emulators_scan_configs` |
| GET | `/api/v2/emulators/settings` | `_api_get_api_v2_emulators_settings` |
| POST | `/api/v2/emulators/settings` | `_api_post_api_v2_emulators_settings` |
| POST | `/api/v2/emulators/update` | `_api_post_api_emulators_update` |
| POST | `/api/v2/emulators/update-all` | `_api_post_api_emulators_update_all` |
| POST | `/api/v2/emumovies/download` | `_api_post_api_emumovies_download` |
| POST | `/api/v2/emumovies/settings` | `_api_post_api_emumovies_settings` |
| GET | `/api/v2/events` | `_api_get_api_events` |
| GET | `/api/v2/explorer/facets` | `_api_get_api_explorer_facets` |
| POST | `/api/v2/extra/launch` | `_api_post_api_extra_launch` |
| POST | `/api/v2/faugus/import` | `_api_post_api_faugus_import` |
| GET | `/api/v2/faugus/scan` | `_api_get_api_faugus_scan` |
| GET | `/api/v2/faugus/status` | `_api_get_api_faugus_status` |
| POST | `/api/v2/favorite` | `_api_post_api_favorite` |
| GET | `/api/v2/filter-presets` | `_api_get_api_filter_presets` |
| POST | `/api/v2/filter-presets` | `_api_post_api_filter_presets` |
| POST | `/api/v2/filter-presets/delete` | `_api_post_api_filter_presets_delete` |
| POST | `/api/v2/game` | `_api_post_api_game` |
| POST | `/api/v2/game/delete` | `_api_post_api_game_delete` |
| POST | `/api/v2/game/store-action` | `_api_post_api_game_store_action` |
| POST | `/api/v2/games/bulk` | `_api_post_api_games_bulk` |
| POST | `/api/v2/games/bulk-wizard` | `_api_post_api_games_bulk_wizard` |
| POST | `/api/v2/games/delete-steam` | `_api_post_api_games_delete_steam` |
| POST | `/api/v2/gameyfin/install` | `_api_post_api_gameyfin_install` |
| GET | `/api/v2/gameyfin/install/status` | `_api_get_api_gameyfin_install_status` |
| GET | `/api/v2/gameyfin/providers` | `_api_get_api_gameyfin_providers` |
| POST | `/api/v2/gameyfin/test` | `_api_post_api_gameyfin_test` |
| POST | `/api/v2/gameyfin/uninstall` | `_api_post_api_gameyfin_uninstall` |
| POST | `/api/v2/health` | `_api_post_api_health` |
| POST | `/api/v2/health/dedupe` | `_api_post_api_health_dedupe` |
| GET | `/api/v2/highscores` | `_api_get_api_highscores` |
| POST | `/api/v2/highscores/export` | `_api_post_api_highscores_export` |
| POST | `/api/v2/highscores/import` | `_api_post_api_highscores_import` |
| GET | `/api/v2/highscores/table` | `_api_get_api_v2_highscores_table` |
| GET | `/api/v2/history` | `_api_get_api_history` |
| GET | `/api/v2/history/timeline` | `_api_get_api_v2_history_timeline` |
| GET | `/api/v2/household` | `handlers.household.household_status` |
| POST | `/api/v2/household/challenge` | `handlers.household.household_challenge` |
| POST | `/api/v2/household/challenge/result` | `handlers.household.household_result` |
| GET | `/api/v2/household/leaderboard` | `handlers.household.household_leaderboard` |
| POST | `/api/v2/household/member` | `handlers.household.household_member` |
| POST | `/api/v2/household/merge` | `handlers.household.household_merge` |
| POST | `/api/v2/household/record` | `handlers.household.household_record` |
| POST | `/api/v2/household/share` | `handlers.household.household_share` |
| POST | `/api/v2/household/sync/publish` | `handlers.household.household_sync_publish` |
| POST | `/api/v2/household/sync/pull` | `handlers.household.household_sync_pull` |
| POST | `/api/v2/image-group` | `_api_post_api_image_group` |
| POST | `/api/v2/import` | `_api_post_api_import` |
| POST | `/api/v2/import/arcade` | `_api_post_api_import_arcade` |
| POST | `/api/v2/import/ea` | `_api_post_api_v2_import_ea` |
| POST | `/api/v2/import/epic` | `_api_post_api_import_epic` |
| POST | `/api/v2/import/esde/apply` | `_api_post_api_v2_import_esde_apply` |
| POST | `/api/v2/import/esde/preview` | `_api_post_api_v2_import_esde_preview` |
| GET | `/api/v2/import/exclusions` | `_api_get_api_import_exclusions` |
| POST | `/api/v2/import/exclusions` | `_api_post_api_import_exclusions` |
| POST | `/api/v2/import/exclusions/delete` | `_api_post_api_import_exclusions_delete` |
| POST | `/api/v2/import/gog` | `_api_post_api_v2_import_gog` |
| POST | `/api/v2/import/heroic` | `_api_post_api_import_heroic` |
| POST | `/api/v2/import/launchbox/apply` | `_api_post_api_v2_import_launchbox_apply` |
| POST | `/api/v2/import/launchbox/preview` | `_api_post_api_v2_import_launchbox_preview` |
| POST | `/api/v2/import/loose-arcade` | `_api_post_api_import_loose_arcade` |
| POST | `/api/v2/import/lutris` | `_api_post_api_import_lutris` |
| POST | `/api/v2/import/msu1` | `_api_post_api_v2_import_msu1` |
| POST | `/api/v2/import/pico8` | `_api_post_api_v2_import_pico8` |
| POST | `/api/v2/import/rpcs3` | `_api_post_api_import_rpcs3` |
| POST | `/api/v2/import/scummvm` | `_api_post_api_import_scummvm` |
| POST | `/api/v2/import/steam` | `_api_post_api_import_steam` |
| POST | `/api/v2/import/ubisoft` | `_api_post_api_v2_import_ubisoft` |
| POST | `/api/v2/import/vita3k` | `_api_post_api_import_vita3k` |
| POST | `/api/v2/import/watch` | `_api_post_api_import_watch` |
| POST | `/api/v2/import/whdload` | `_api_post_api_v2_import_whdload` |
| POST | `/api/v2/import/wizard` | `_api_post_api_import_wizard` |
| POST | `/api/v2/import/xbox360` | `_api_post_api_import_xbox360` |
| GET | `/api/v2/insights/heatmap` | `_api_get_api_v2_insights_heatmap` |
| GET | `/api/v2/insights/mastery` | `_api_get_api_v2_insights_mastery` |
| GET | `/api/v2/insights/radar` | `_api_get_api_v2_insights_radar` |
| POST | `/api/v2/insights/radar/park` | `_api_post_api_v2_insights_radar_park` |
| GET | `/api/v2/insights/radio` | `_api_get_api_v2_insights_radio` |
| POST | `/api/v2/insights/radio/refresh` | `_api_post_api_v2_insights_radio_refresh` |
| GET | `/api/v2/insights/summary` | `_api_get_api_v2_insights_summary` |
| GET | `/api/v2/insights/trophies` | `_api_get_api_v2_insights_trophies` |
| POST | `/api/v2/insights/trophies/evaluate` | `_api_post_api_v2_insights_trophies_evaluate` |
| GET | `/api/v2/insights/wrapped` | `_api_get_api_v2_insights_wrapped` |
| GET | `/api/v2/integration-tokens` | `_api_get_api_v2_integration_tokens` |
| POST | `/api/v2/integration-tokens` | `_api_post_api_v2_integration_tokens` |
| POST | `/api/v2/integration-tokens/remove` | `_api_post_api_v2_integration_tokens_remove` |
| GET | `/api/v2/integrations/steam` | `_api_get_api_v2_integrations_steam` |
| POST | `/api/v2/integrations/steam` | `_api_post_api_v2_integrations_steam` |
| POST | `/api/v2/integrations/steam/refresh` | `_api_post_api_v2_integrations_steam_refresh` |
| POST | `/api/v2/integrations/steam/test` | `_api_post_api_v2_integrations_steam_test` |
| GET | `/api/v2/jobs` | `_api_get_api_v2_jobs` |
| POST | `/api/v2/jobs/cancel` | `_api_post_api_v2_jobs_cancel` |
| GET | `/api/v2/jobs/items` | `_api_get_api_v2_jobs_items` |
| POST | `/api/v2/jobs/resume` | `_api_post_api_v2_jobs_resume` |
| POST | `/api/v2/jobs/retry` | `_api_post_api_v2_jobs_retry` |
| POST | `/api/v2/launch` | `_api_post_api_launch` |
| GET | `/api/v2/launch/audit` | `_api_get_api_v2_launch_audit` |
| POST | `/api/v2/launch/audit/refresh` | `_api_post_api_v2_launch_audit_refresh` |
| POST | `/api/v2/launch/audit/scan` | `_api_post_api_v2_launch_audit_scan` |
| GET | `/api/v2/launch/audit/status` | `_api_get_api_v2_launch_audit_status` |
| POST | `/api/v2/launch/core` | `_api_post_api_v2_launch_core` |
| GET | `/api/v2/launch/cores` | `_api_get_api_v2_launch_cores` |
| POST | `/api/v2/launch/grant` | `_api_post_api_v2_launch_grant` |
| POST | `/api/v2/launch/grant/undo` | `_api_post_api_v2_launch_grant_undo` |
| POST | `/api/v2/launch/preflight` | `_api_post_api_v2_launch_preflight` |
| POST | `/api/v2/launch/preflight/batch` | `_api_post_api_v2_launch_preflight_batch` |
| GET | `/api/v2/launcher/menu` | `_api_get_api_launcher_menu` |
| GET | `/api/v2/library` | `_api_get_api_library` |
| GET | `/api/v2/library/constellation` | `_api_get_api_v2_library_constellation` |
| GET | `/api/v2/library/delta` | `_api_get_api_library_delta` |
| POST | `/api/v2/library/dna/index/rebuild` | `_api_post_api_v2_library_dna_index_rebuild` |
| POST | `/api/v2/library/dna/search` | `_api_post_api_v2_library_dna_search` |
| GET | `/api/v2/library/dna/status` | `_api_get_api_v2_library_dna_status` |
| GET | `/api/v2/library/duplicates` | `_api_get_api_v2_library_duplicates` |
| POST | `/api/v2/library/duplicates/merge` | `_api_post_api_v2_library_duplicates_merge` |
| POST | `/api/v2/library/duplicates/preview` | `_api_post_api_v2_library_duplicates_preview` |
| POST | `/api/v2/library/export` | `_api_post_api_v2_library_export` |
| GET | `/api/v2/library/export/download` | `_api_get_api_v2_library_export_download` |
| GET | `/api/v2/library/export/exports` | `_api_get_api_v2_library_export_exports` |
| GET | `/api/v2/library/health` | `handlers.library_health.health_snapshot` |
| POST | `/api/v2/library/health/fix` | `handlers.library_health.health_fix` |
| GET | `/api/v2/library/health/issues` | `handlers.library_health.health_issues` |
| POST | `/api/v2/library/health/scan` | `handlers.library_health.health_scan` |
| POST | `/api/v2/library/health/undo` | `handlers.library_health.health_undo` |
| GET | `/api/v2/library/integrity` | `handlers.library_storage.integrity` |
| POST | `/api/v2/library/manual-entry` | `_api_post_api_v2_library_manual_entry` |
| POST | `/api/v2/library/manual-entry/convert` | `_api_post_api_v2_library_manual_entry_convert` |
| POST | `/api/v2/library/manual-entry/update` | `_api_post_api_v2_library_manual_entry_update` |
| POST | `/api/v2/library/move/apply` | `handlers.library_storage.move_apply` |
| POST | `/api/v2/library/move/preview` | `handlers.library_storage.move_preview` |
| POST | `/api/v2/library/notes/add` | `_api_post_api_v2_library_notes_add` |
| POST | `/api/v2/library/notes/delete` | `_api_post_api_v2_library_notes_delete` |
| POST | `/api/v2/library/notes/update` | `_api_post_api_v2_library_notes_update` |
| POST | `/api/v2/library/pick` | `_api_post_api_v2_library_pick` |
| POST | `/api/v2/library/playtime/delete` | `_api_post_api_v2_library_playtime_delete` |
| POST | `/api/v2/library/playtime/log` | `_api_post_api_v2_library_playtime_log` |
| POST | `/api/v2/library/playtime/update` | `_api_post_api_v2_library_playtime_update` |
| POST | `/api/v2/library/progress/set` | `_api_post_api_v2_library_progress_set` |
| POST | `/api/v2/library/query/parse` | `_api_post_api_v2_library_query_parse` |
| POST | `/api/v2/library/rating/set` | `_api_post_api_v2_library_rating_set` |
| GET | `/api/v2/library/repair` | `_api_get_api_v2_library_repair` |
| POST | `/api/v2/library/repair/apply` | `_api_post_api_v2_library_repair_apply` |
| POST | `/api/v2/library/repair/preview` | `_api_post_api_v2_library_repair_preview` |
| GET | `/api/v2/library/search` | `_api_get_api_v2_library_search` |
| POST | `/api/v2/library/sync/apply` | `_api_post_api_v2_library_sync_apply` |
| POST | `/api/v2/library/sync/preview` | `_api_post_api_v2_library_sync_preview` |
| POST | `/api/v2/library/sync/publish` | `_api_post_api_v2_library_sync_publish` |
| POST | `/api/v2/library/sync/pull` | `_api_post_api_v2_library_sync_pull` |
| GET | `/api/v2/library/time-machine/as-of` | `_api_get_api_v2_library_time_machine_as_of` |
| GET | `/api/v2/library/time-machine/compare` | `_api_get_api_v2_library_time_machine_compare` |
| GET | `/api/v2/library/time-machine/events` | `_api_get_api_v2_library_time_machine_events` |
| POST | `/api/v2/library/time-machine/revert` | `_api_post_api_v2_library_time_machine_revert` |
| GET | `/api/v2/library/trash` | `_api_get_api_v2_library_trash` |
| POST | `/api/v2/library/trash` | `_api_post_api_v2_library_trash` |
| POST | `/api/v2/library/trash/purge` | `_api_post_api_v2_library_trash_purge` |
| POST | `/api/v2/library/trash/restore` | `_api_post_api_v2_library_trash_restore` |
| GET | `/api/v2/log` | `_api_get_api_log` |
| GET | `/api/v2/media` | `_api_get_api_media` |
| GET | `/api/v2/media/audit` | `_api_get_api_media_audit` |
| POST | `/api/v2/media/bulk` | `_api_post_api_media_bulk` |
| GET | `/api/v2/media/bulk/status` | `_api_get_api_media_bulk_status` |
| POST | `/api/v2/media/cache/cleanup` | `_api_post_api_media_cache_cleanup` |
| POST | `/api/v2/media/cleanup` | `_api_post_api_media_cleanup` |
| GET | `/api/v2/media/duplicates` | `_api_get_api_media_duplicates` |
| GET | `/api/v2/media/queue` | `_api_get_api_media_queue` |
| POST | `/api/v2/media/queue/cancel` | `_api_post_api_media_queue_cancel` |
| POST | `/api/v2/media/queue/pause` | `_api_post_api_media_queue_pause` |
| POST | `/api/v2/media/queue/resume` | `_api_post_api_media_queue_resume` |
| POST | `/api/v2/media/queue/retry` | `_api_post_api_media_queue_retry` |
| GET | `/api/v2/memories` | `_api_get_api_v2_memories` |
| POST | `/api/v2/memories/import` | `_api_post_api_v2_memories_import` |
| GET | `/api/v2/memories/media` | `_api_get_api_v2_memories_media` |
| GET | `/api/v2/memories/status` | `_api_get_api_v2_memories_status` |
| POST | `/api/v2/metadata/apply` | `_api_post_api_metadata_apply` |
| POST | `/api/v2/metadata/auto-scrape` | `handlers.metadata.metadata_auto_scrape` |
| POST | `/api/v2/metadata/candidates/apply` | `_api_post_api_v2_metadata_candidates_apply` |
| POST | `/api/v2/metadata/gog` | `_api_post_api_metadata_gog` |
| POST | `/api/v2/metadata/igdb/apply` | `_api_post_api_metadata_igdb_apply` |
| GET | `/api/v2/metadata/igdb/search` | `_api_get_api_metadata_igdb_search` |
| POST | `/api/v2/metadata/match` | `_api_post_api_metadata_match` |
| POST | `/api/v2/metadata/matches/apply` | `_api_post_api_v2_metadata_matches_apply` |
| POST | `/api/v2/metadata/matches/decisions` | `_api_post_api_v2_metadata_matches_decisions` |
| GET | `/api/v2/metadata/matches/items` | `_api_get_api_v2_metadata_matches_items` |
| GET | `/api/v2/metadata/matches/preview` | `_api_get_api_v2_metadata_matches_preview` |
| POST | `/api/v2/metadata/matches/preview` | `_api_post_api_v2_metadata_matches_preview` |
| GET | `/api/v2/metadata/media-candidates` | `_api_get_api_v2_metadata_media_candidates` |
| GET | `/api/v2/metadata/scrape-settings` | `handlers.metadata._api_get_api_v2_metadata_scrape_settings` |
| POST | `/api/v2/metadata/scrape-settings` | `handlers.metadata._api_post_api_v2_metadata_scrape_settings` |
| GET | `/api/v2/metadata/search` | `_api_get_api_metadata_search` |
| GET | `/api/v2/metadata/status` | `_api_get_api_metadata_status` |
| POST | `/api/v2/metadata/steam` | `_api_post_api_metadata_steam` |
| POST | `/api/v2/metadata/sync` | `_api_post_api_metadata_sync` |
| POST | `/api/v2/metadata/trailer` | `_api_post_api_metadata_trailer` |
| GET | `/api/v2/moments` | `handlers.moments.moments_list` |
| POST | `/api/v2/moments` | `handlers.moments.moments_create` |
| POST | `/api/v2/moments/delete` | `handlers.moments.moments_delete` |
| POST | `/api/v2/moments/resume` | `handlers.moments.moments_resume` |
| POST | `/api/v2/moments/update` | `handlers.moments.moments_update` |
| GET | `/api/v2/native/battery` | `handlers.native.battery` |
| GET | `/api/v2/native/capabilities` | `handlers.native.capabilities` |
| POST | `/api/v2/native/dialog` | `handlers.native.dialog` |
| POST | `/api/v2/native/open-external` | `handlers.native.open_external` |
| POST | `/api/v2/native/reveal` | `handlers.native.reveal` |
| POST | `/api/v2/native/window` | `handlers.native.window` |
| GET | `/api/v2/netplay/lobby` | `_api_get_api_v2_netplay_lobby` |
| GET | `/api/v2/notifications` | `_api_get_api_notifications` |
| POST | `/api/v2/notifications` | `_api_post_api_notifications` |
| POST | `/api/v2/obs/attach` | `_api_post_api_obs_attach` |
| GET | `/api/v2/obs/status` | `_api_get_api_obs_status` |
| GET | `/api/v2/party/decks` | `_api_get_api_v2_party_decks` |
| POST | `/api/v2/party/decks` | `_api_post_api_v2_party_decks` |
| POST | `/api/v2/party/decks/delete` | `_api_post_api_v2_party_decks_delete` |
| POST | `/api/v2/party/next` | `_api_post_api_v2_party_next` |
| GET | `/api/v2/party/queue` | `_api_get_api_v2_party_queue` |
| POST | `/api/v2/party/queue` | `_api_post_api_v2_party_queue` |
| GET | `/api/v2/perf_profiles` | `_api_get_api_perf_profiles` |
| POST | `/api/v2/perf_profiles` | `_api_post_api_perf_profiles` |
| POST | `/api/v2/platform/delete` | `_api_post_api_v2_platform_delete` |
| GET | `/api/v2/platform/document` | `_api_get_api_platform_document` |
| GET | `/api/v2/platform/documents` | `_api_get_api_platform_documents` |
| POST | `/api/v2/platform/documents` | `_api_post_api_platform_documents` |
| POST | `/api/v2/platform/rename` | `_api_post_api_v2_platform_rename` |
| GET | `/api/v2/platform/video` | `_api_get_api_platform_video` |
| GET | `/api/v2/platform/videos` | `_api_get_api_platform_videos` |
| POST | `/api/v2/platform/videos` | `_api_post_api_platform_videos` |
| POST | `/api/v2/playlists` | `_api_post_api_playlists` |
| POST | `/api/v2/playlists/delete` | `_api_post_api_playlists_delete` |
| GET | `/api/v2/plugin-stats` | `_api_get_api_v2_plugin_stats` |
| POST | `/api/v2/plugin-stats/refresh` | `_api_post_api_v2_plugin_stats_refresh` |
| GET | `/api/v2/plugins` | `_api_get_api_plugins` |
| GET | `/api/v2/plugins/catalog` | `_api_get_api_v2_plugins_catalog` |
| POST | `/api/v2/plugins/catalog/install` | `_api_post_api_plugins_catalog_install` |
| POST | `/api/v2/plugins/command` | `_api_post_api_v2_plugins_command` |
| GET | `/api/v2/plugins/commands` | `_api_get_api_v2_plugins_commands` |
| GET | `/api/v2/plugins/credentials` | `_api_get_api_v2_plugins_credentials` |
| POST | `/api/v2/plugins/credentials` | `_api_post_api_v2_plugins_credentials` |
| GET | `/api/v2/plugins/data` | `_api_get_api_v2_plugins_data` |
| POST | `/api/v2/plugins/data` | `_api_post_api_v2_plugins_data` |
| POST | `/api/v2/plugins/install` | `_api_post_api_plugins_install` |
| POST | `/api/v2/plugins/permissions` | `_api_post_api_v2_plugins_permissions` |
| POST | `/api/v2/plugins/remove` | `_api_post_api_plugins_remove` |
| POST | `/api/v2/plugins/repair` | `_api_post_api_v2_plugins_repair` |
| GET | `/api/v2/plugins/settings` | `_api_get_api_v2_plugins_settings` |
| POST | `/api/v2/plugins/settings` | `_api_post_api_v2_plugins_settings` |
| POST | `/api/v2/plugins/toggle` | `_api_post_api_plugins_toggle` |
| GET | `/api/v2/plugins/trust` | `_api_get_api_v2_plugins_trust` |
| POST | `/api/v2/plugins/trust` | `_api_post_api_v2_plugins_trust` |
| GET | `/api/v2/premium/media-packs` | `_api_get_api_premium_media_packs` |
| POST | `/api/v2/premium/media-packs/apply` | `_api_post_api_premium_media_packs_apply` |
| GET | `/api/v2/premium/platform-categories` | `_api_get_api_premium_platform_categories` |
| GET | `/api/v2/premium/strings` | `_api_get_api_premium_strings` |
| GET | `/api/v2/profiles` | `_api_get_api_profiles` |
| POST | `/api/v2/profiles` | `_api_post_api_profiles` |
| GET | `/api/v2/queue` | `_api_get_api_queue` |
| POST | `/api/v2/queue` | `_api_post_api_queue` |
| GET | `/api/v2/ra/badge` | `_api_get_api_ra_badge` |
| POST | `/api/v2/ra/game` | `_api_post_api_ra_game` |
| POST | `/api/v2/ra/inject` | `_api_post_api_ra_inject` |
| GET | `/api/v2/ra/settings` | `_api_get_api_ra_settings` |
| POST | `/api/v2/ra/settings` | `_api_post_api_ra_settings` |
| GET | `/api/v2/reels` | `handlers.clips.reel_manifest` |
| POST | `/api/v2/reels/create` | `handlers.clips.create_reel_job` |
| GET | `/api/v2/related` | `_api_get_api_related` |
| GET | `/api/v2/related/rich` | `_api_get_api_related_rich` |
| POST | `/api/v2/resume` | `handlers.resume.resume_launch` |
| POST | `/api/v2/resume/discard` | `handlers.resume.resume_discard` |
| POST | `/api/v2/resume/slot` | `handlers.resume.resume_slot` |
| GET | `/api/v2/resume/status` | `handlers.resume.resume_status` |
| GET | `/api/v2/running` | `_api_get_api_running` |
| POST | `/api/v2/save-tools/hoard` | `_api_post_api_save_tools_hoard` |
| POST | `/api/v2/save-tools/ludusavi` | `_api_post_api_save_tools_ludusavi` |
| GET | `/api/v2/save-tools/status` | `_api_get_api_save_tools_status` |
| GET | `/api/v2/saves` | `_api_get_api_saves` |
| POST | `/api/v2/saves/add` | `_api_post_api_saves_add` |
| POST | `/api/v2/saves/backup` | `_api_post_api_saves_backup` |
| GET | `/api/v2/saves/discover` | `_api_get_api_saves_discover` |
| GET | `/api/v2/saves/manifest` | `_api_get_api_saves_manifest` |
| POST | `/api/v2/saves/restore` | `_api_post_api_saves_restore` |
| POST | `/api/v2/saves/restore/preview` | `_api_post_api_saves_restore_preview` |
| GET | `/api/v2/saves/scan` | `_api_get_api_saves_scan` |
| POST | `/api/v2/saves/scan/apply` | `_api_post_api_saves_scan_apply` |
| POST | `/api/v2/screenscraper/apply` | `_api_post_api_v2_screenscraper_apply` |
| POST | `/api/v2/screenscraper/info` | `_api_post_api_v2_screenscraper_info` |
| POST | `/api/v2/screenscraper/match` | `_api_post_api_v2_screenscraper_match` |
| GET | `/api/v2/screenscraper/search` | `_api_get_api_v2_screenscraper_search` |
| GET | `/api/v2/screenscraper/status` | `_api_get_api_v2_screenscraper_status` |
| POST | `/api/v2/screenscraper/test` | `_api_post_api_v2_screenscraper_test` |
| POST | `/api/v2/screenshot` | `_api_post_api_screenshot` |
| POST | `/api/v2/session/cleanup` | `_api_post_api_session_cleanup` |
| POST | `/api/v2/session/control` | `_api_post_api_session_control` |
| GET | `/api/v2/sessions/recap` | `_api_get_api_v2_sessions_recap` |
| GET | `/api/v2/settings` | `_api_get_api_settings` |
| POST | `/api/v2/settings` | `_api_post_api_settings` |
| POST | `/api/v2/setup/commit` | `_api_post_api_v2_setup_commit` |
| POST | `/api/v2/setup/migration/acknowledge` | `handlers.setup.migration_acknowledge` |
| POST | `/api/v2/setup/migration/backup` | `handlers.setup.migration_backup` |
| POST | `/api/v2/setup/migration/rollback` | `handlers.setup.migration_rollback` |
| GET | `/api/v2/setup/migration/status` | `handlers.setup.migration_status` |
| GET | `/api/v2/setup/preview` | `_api_get_api_v2_setup_preview` |
| POST | `/api/v2/setup/preview` | `_api_post_api_v2_setup_preview` |
| POST | `/api/v2/setup/preview/decisions` | `_api_post_api_v2_setup_preview_decisions` |
| GET | `/api/v2/setup/preview/items` | `_api_get_api_v2_setup_preview_items` |
| POST | `/api/v2/setup/preview/revalidate` | `_api_post_api_v2_setup_preview_revalidate` |
| GET | `/api/v2/setup/summary` | `_api_get_api_v2_setup_summary` |
| POST | `/api/v2/shutdown` | `_api_post_api_shutdown` |
| POST | `/api/v2/state/recover` | `_api_post_api_state_recover` |
| POST | `/api/v2/steam/action` | `_api_post_api_v2_steam_action` |
| POST | `/api/v2/steambridge/apply` | `handlers.steambridge.steambridge_apply` |
| POST | `/api/v2/steambridge/preview` | `handlers.steambridge.steambridge_preview` |
| POST | `/api/v2/steambridge/remove` | `handlers.steambridge.steambridge_remove` |
| POST | `/api/v2/steambridge/remove/preview` | `handlers.steambridge.steambridge_remove_preview` |
| GET | `/api/v2/steambridge/status` | `handlers.steambridge.steambridge_status` |
| POST | `/api/v2/steamgrid/apply` | `handlers.steamgrid.steamgrid_apply` |
| POST | `/api/v2/steamgrid/hygiene/fix` | `handlers.steamgrid.steamgrid_hygiene_fix` |
| GET | `/api/v2/steamgrid/hygiene/report` | `handlers.steamgrid.steamgrid_hygiene_report` |
| POST | `/api/v2/steamgrid/hygiene/undo` | `handlers.steamgrid.steamgrid_hygiene_undo` |
| POST | `/api/v2/steamgrid/info` | `handlers.steamgrid.steamgrid_info` |
| POST | `/api/v2/steamgrid/match` | `handlers.steamgrid.steamgrid_match` |
| GET | `/api/v2/steamgrid/search` | `handlers.steamgrid.steamgrid_search` |
| GET | `/api/v2/steamgrid/status` | `handlers.steamgrid.steamgrid_status` |
| POST | `/api/v2/steamgrid/test` | `handlers.steamgrid.steamgrid_test` |
| GET | `/api/v2/storefront/catalog` | `_api_get_api_storefront_catalog` |
| POST | `/api/v2/storefront/import` | `_api_post_api_storefront_import` |
| POST | `/api/v2/storefront/scan` | `_api_post_api_v2_storefront_scan` |
| GET | `/api/v2/storefront/status` | `_api_get_api_v2_storefront_status` |
| GET | `/api/v2/storefronts` | `_api_get_api_v2_storefronts` |
| POST | `/api/v2/storefronts/plugin/catalog` | `_api_post_api_v2_storefronts_plugin_catalog` |
| POST | `/api/v2/storefronts/plugin/import` | `_api_post_api_v2_storefronts_plugin_import` |
| GET | `/api/v2/story` | `_api_get_api_v2_story` |
| GET | `/api/v2/tags` | `_api_get_api_tags` |
| POST | `/api/v2/tags` | `_api_post_api_tags` |
| GET | `/api/v2/theme-asset` | `_api_get_api_theme_asset` |
| GET | `/api/v2/theme-sounds` | `_api_get_api_theme_sounds` |
| GET | `/api/v2/theme.css` | `_api_get_api_theme_css` |
| GET | `/api/v2/themes` | `_api_get_api_themes` |
| POST | `/api/v2/themes/import` | `_api_post_api_themes_import` |
| POST | `/api/v2/themes/open-folder` | `_api_post_api_themes_open_folder` |
| POST | `/api/v2/themes/select` | `_api_post_api_themes_select` |
| GET | `/api/v2/update` | `_api_get_api_update` |
| POST | `/api/v2/update/install` | `_api_post_api_update_install` |
| GET | `/api/v2/version` | `handlers.native.version_info` |
| GET | `/api/v2/webhooks` | `_api_get_api_webhooks` |
| POST | `/api/v2/webhooks` | `_api_post_api_webhooks` |
| POST | `/api/v2/webhooks/test` | `_api_post_api_webhooks_test` |
| GET | `/api/v2/wine/prefix-for-game` | `_api_get_api_wine_prefix_for_game` |
| GET | `/api/v2/wine/prefixes` | `_api_get_api_wine_prefixes` |
| GET | `/api/v2/wine/protons` | `_api_get_api_wine_protons` |

_387 routes._

## API v1 (frozen)

The v1 surface is the stable contract. Legacy `/api/*` paths stay available for older clients; additive feature work targets `/api/v2/*` so the frozen v1 contract does not drift.

| Method | Path | Handler | Response |
|---|---|---|---|
| - | `/api/v1/backup` | `-` | `-` |
| - | `/api/v1/backup/create` | `-` | `-` |
| - | `/api/v1/backup/restore` | `-` | `-` |
| - | `/api/v1/backups` | `-` | `-` |
| - | `/api/v1/bigbox/mode` | `-` | `-` |
| - | `/api/v1/diagnostic` | `-` | `-` |
| - | `/api/v1/emulators` | `-` | `-` |
| - | `/api/v1/emulators/install` | `-` | `-` |
| - | `/api/v1/extra/launch` | `-` | `-` |
| - | `/api/v1/favorite` | `-` | `-` |
| - | `/api/v1/filter-presets` | `-` | `-` |
| - | `/api/v1/game` | `-` | `saved game record` |
| - | `/api/v1/game/delete` | `-` | `removed game name` |
| - | `/api/v1/games/bulk` | `-` | `-` |
| - | `/api/v1/games/bulk-wizard` | `-` | `-` |
| - | `/api/v1/gameyfin/test` | `-` | `-` |
| - | `/api/v1/health` | `-` | `health facts and job history` |
| - | `/api/v1/health/dedupe` | `-` | `-` |
| - | `/api/v1/history` | `-` | `enabled, history` |
| - | `/api/v1/import` | `-` | `added, found, recommendations` |
| - | `/api/v1/import/arcade` | `-` | `-` |
| - | `/api/v1/import/epic` | `-` | `-` |
| - | `/api/v1/import/heroic` | `-` | `-` |
| - | `/api/v1/import/lutris` | `-` | `-` |
| - | `/api/v1/import/rpcs3` | `-` | `-` |
| - | `/api/v1/import/scummvm` | `-` | `-` |
| - | `/api/v1/import/steam` | `-` | `-` |
| - | `/api/v1/import/vita3k` | `-` | `-` |
| - | `/api/v1/jobs` | `-` | `jobs, history` |
| - | `/api/v1/launch` | `-` | `ok, launch_id, game` |
| - | `/api/v1/library` | `-` | `games, playlists, settings, platforms, categories, tags, queue` |
| - | `/api/v1/log` | `-` | `-` |
| - | `/api/v1/media` | `-` | `media bytes or a JSON error` |
| - | `/api/v1/media/audit` | `-` | `-` |
| - | `/api/v1/media/bulk` | `-` | `media job state` |
| - | `/api/v1/media/cleanup` | `-` | `-` |
| - | `/api/v1/metadata/apply` | `-` | `applied field names and notes` |
| - | `/api/v1/metadata/match` | `-` | `-` |
| - | `/api/v1/metadata/search` | `-` | `-` |
| - | `/api/v1/metadata/status` | `-` | `ready, coverage, matched counts` |
| - | `/api/v1/notifications` | `-` | `notifications and unread count` |
| - | `/api/v1/playlists` | `-` | `saved playlist name` |
| - | `/api/v1/plugins` | `-` | `-` |
| - | `/api/v1/premium/media-packs` | `-` | `-` |
| - | `/api/v1/premium/media-packs/apply` | `-` | `-` |
| - | `/api/v1/profiles` | `-` | `emulator profile map` |
| - | `/api/v1/queue` | `-` | `queue and the advanced game` |
| - | `/api/v1/ra/inject` | `-` | `-` |
| - | `/api/v1/running` | `-` | `running, events, last_event` |
| - | `/api/v1/saves` | `-` | `discovered save backups` |
| - | `/api/v1/saves/scan/apply` | `-` | `-` |
| - | `/api/v1/settings` | `-` | `the full public settings object` |
| - | `/api/v1/shutdown` | `-` | `-` |
| - | `/api/v1/state/recover` | `-` | `-` |
| - | `/api/v1/storefront/import` | `-` | `-` |
| - | `/api/v1/tags` | `-` | `updated count and tag counts` |
| - | `/api/v1/themes` | `-` | `installed themes and the active theme` |
| - | `/api/v1/themes/open-folder` | `-` | `-` |
| - | `/api/v1/update` | `-` | `available, latest, current` |
| - | `/api/v1/update/install` | `-` | `-` |
| - | `/api/v1/webhooks` | `-` | `saved webhooks and event types` |

_61 routes._

