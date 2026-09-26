# API reference

Generated from `routes.py`, `contracts.py`, and `routes/registry.py`; do not edit by hand. Regenerate with `python3 scripts/gen_api_docs.py --v2`.

Authentication: requests use the `X-OpenBox-Token` header with the per-process token from `server.token`; the local UI may also use a query token during startup. Public assets and byte/stream endpoints follow their route-specific behavior.

## API v2

The additive surface for 1.13+ features. New work targets `/api/v2/*`; routes listed here may evolve until they are explicitly frozen.

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v2/arcade/kiosk/pin` | `handlers.arcade.kiosk_pin` |
| GET | `/api/v2/arcade/kiosk/status` | `handlers.arcade.kiosk_status` |
| POST | `/api/v2/arcade/kiosk/verify` | `handlers.arcade.kiosk_verify` |
| GET | `/api/v2/backup/diff` | `_api_get_api_v2_backup_diff` |
| GET | `/api/v2/clips` | `handlers.clips.clips_list` |
| POST | `/api/v2/clips/capture` | `handlers.clips.capture_clip` |
| GET | `/api/v2/collections` | `handlers.collections.collections_list` |
| POST | `/api/v2/collections` | `handlers.collections.collections_save` |
| POST | `/api/v2/collections/delete` | `handlers.collections.collections_delete` |
| POST | `/api/v2/emulators/defs/rollback` | `handlers.defs._api_post_api_v2_emulators_defs_rollback` |
| GET | `/api/v2/emulators/defs/status` | `handlers.defs._api_get_api_v2_emulators_defs_status` |
| GET | `/api/v2/emulators/defs/update` | `handlers.defs._api_get_api_v2_emulators_defs_update` |
| POST | `/api/v2/emulators/defs/update` | `handlers.defs._api_post_api_v2_emulators_defs_update` |
| GET | `/api/v2/emulators/registry` | `_api_get_api_v2_emulators_registry` |
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
| POST | `/api/v2/import/esde/apply` | `_api_post_api_v2_import_esde_apply` |
| POST | `/api/v2/import/esde/preview` | `_api_post_api_v2_import_esde_preview` |
| POST | `/api/v2/import/launchbox/apply` | `_api_post_api_v2_import_launchbox_apply` |
| POST | `/api/v2/import/launchbox/preview` | `_api_post_api_v2_import_launchbox_preview` |
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
| GET | `/api/v2/jobs` | `_api_get_api_v2_jobs` |
| POST | `/api/v2/jobs/cancel` | `_api_post_api_v2_jobs_cancel` |
| GET | `/api/v2/jobs/items` | `_api_get_api_v2_jobs_items` |
| POST | `/api/v2/jobs/resume` | `_api_post_api_v2_jobs_resume` |
| POST | `/api/v2/jobs/retry` | `_api_post_api_v2_jobs_retry` |
| POST | `/api/v2/launch/preflight` | `_api_post_api_v2_launch_preflight` |
| POST | `/api/v2/launch/preflight/batch` | `_api_post_api_v2_launch_preflight_batch` |
| GET | `/api/v2/library/constellation` | `_api_get_api_v2_library_constellation` |
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
| POST | `/api/v2/library/manual-entry` | `_api_post_api_v2_library_manual_entry` |
| POST | `/api/v2/library/manual-entry/convert` | `_api_post_api_v2_library_manual_entry_convert` |
| POST | `/api/v2/library/manual-entry/update` | `_api_post_api_v2_library_manual_entry_update` |
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
| GET | `/api/v2/memories` | `_api_get_api_v2_memories` |
| POST | `/api/v2/memories/import` | `_api_post_api_v2_memories_import` |
| GET | `/api/v2/memories/media` | `_api_get_api_v2_memories_media` |
| GET | `/api/v2/memories/status` | `_api_get_api_v2_memories_status` |
| POST | `/api/v2/metadata/auto-scrape` | `handlers.metadata.metadata_auto_scrape` |
| POST | `/api/v2/metadata/matches/apply` | `_api_post_api_v2_metadata_matches_apply` |
| POST | `/api/v2/metadata/matches/decisions` | `_api_post_api_v2_metadata_matches_decisions` |
| GET | `/api/v2/metadata/matches/items` | `_api_get_api_v2_metadata_matches_items` |
| GET | `/api/v2/metadata/matches/preview` | `_api_get_api_v2_metadata_matches_preview` |
| POST | `/api/v2/metadata/matches/preview` | `_api_post_api_v2_metadata_matches_preview` |
| GET | `/api/v2/metadata/media-candidates` | `_api_get_api_v2_metadata_media_candidates` |
| GET | `/api/v2/metadata/scrape-settings` | `handlers.metadata._api_get_api_v2_metadata_scrape_settings` |
| POST | `/api/v2/metadata/scrape-settings` | `handlers.metadata._api_post_api_v2_metadata_scrape_settings` |
| GET | `/api/v2/moments` | `handlers.moments.moments_list` |
| POST | `/api/v2/moments` | `handlers.moments.moments_create` |
| POST | `/api/v2/moments/delete` | `handlers.moments.moments_delete` |
| POST | `/api/v2/moments/resume` | `handlers.moments.moments_resume` |
| POST | `/api/v2/moments/update` | `handlers.moments.moments_update` |
| POST | `/api/v2/party/next` | `_api_post_api_v2_party_next` |
| GET | `/api/v2/party/queue` | `_api_get_api_v2_party_queue` |
| POST | `/api/v2/party/queue` | `_api_post_api_v2_party_queue` |
| GET | `/api/v2/plugins/catalog` | `_api_get_api_v2_plugins_catalog` |
| POST | `/api/v2/plugins/command` | `_api_post_api_v2_plugins_command` |
| GET | `/api/v2/plugins/commands` | `_api_get_api_v2_plugins_commands` |
| POST | `/api/v2/plugins/permissions` | `_api_post_api_v2_plugins_permissions` |
| GET | `/api/v2/plugins/settings` | `_api_get_api_v2_plugins_settings` |
| POST | `/api/v2/plugins/settings` | `_api_post_api_v2_plugins_settings` |
| GET | `/api/v2/plugins/trust` | `_api_get_api_v2_plugins_trust` |
| POST | `/api/v2/plugins/trust` | `_api_post_api_v2_plugins_trust` |
| GET | `/api/v2/reels` | `handlers.clips.reel_manifest` |
| POST | `/api/v2/reels/create` | `handlers.clips.create_reel_job` |
| POST | `/api/v2/resume` | `handlers.resume.resume_launch` |
| POST | `/api/v2/resume/discard` | `handlers.resume.resume_discard` |
| GET | `/api/v2/resume/status` | `handlers.resume.resume_status` |
| POST | `/api/v2/screenscraper/apply` | `_api_post_api_v2_screenscraper_apply` |
| POST | `/api/v2/screenscraper/info` | `_api_post_api_v2_screenscraper_info` |
| POST | `/api/v2/screenscraper/match` | `_api_post_api_v2_screenscraper_match` |
| GET | `/api/v2/screenscraper/search` | `_api_get_api_v2_screenscraper_search` |
| GET | `/api/v2/screenscraper/status` | `_api_get_api_v2_screenscraper_status` |
| POST | `/api/v2/screenscraper/test` | `_api_post_api_v2_screenscraper_test` |
| GET | `/api/v2/sessions/recap` | `_api_get_api_v2_sessions_recap` |
| POST | `/api/v2/setup/commit` | `_api_post_api_v2_setup_commit` |
| GET | `/api/v2/setup/preview` | `_api_get_api_v2_setup_preview` |
| POST | `/api/v2/setup/preview` | `_api_post_api_v2_setup_preview` |
| POST | `/api/v2/setup/preview/decisions` | `_api_post_api_v2_setup_preview_decisions` |
| GET | `/api/v2/setup/preview/items` | `_api_get_api_v2_setup_preview_items` |
| POST | `/api/v2/setup/preview/revalidate` | `_api_post_api_v2_setup_preview_revalidate` |
| GET | `/api/v2/setup/summary` | `_api_get_api_v2_setup_summary` |
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
| GET | `/api/v2/story` | `_api_get_api_v2_story` |

_153 routes._

## API v1 (frozen)

The v1 surface is the stable contract. Legacy `/api/*` paths stay available for older clients; additive feature work targets `/api/v2/*` so the frozen v1 contract does not drift.

| Method | Path | Handler | Response |
|---|---|---|---|
| GET | `/api/v1/backup` | `_api_get_api_backup` | `_api_get_api_backup` |
| POST | `/api/v1/backup/create` | `_api_post_api_backup_create` | `_api_post_api_backup_create` |
| POST | `/api/v1/backup/restore` | `_api_post_api_backup_restore` | `_api_post_api_backup_restore` |
| GET | `/api/v1/backups` | `_api_get_api_backups` | `_api_get_api_backups` |
| POST | `/api/v1/bigbox/mode` | `_api_post_api_bigbox_mode` | `_api_post_api_bigbox_mode` |
| GET | `/api/v1/diagnostic` | `_api_get_api_diagnostic` | `_api_get_api_diagnostic` |
| GET | `/api/v1/emulators` | `_api_get_api_emulators` | `_api_get_api_emulators` |
| POST | `/api/v1/emulators/install` | `_api_post_api_emulators_install` | `_api_post_api_emulators_install` |
| POST | `/api/v1/extra/launch` | `_api_post_api_extra_launch` | `_api_post_api_extra_launch` |
| POST | `/api/v1/favorite` | `_api_post_api_favorite` | `_api_post_api_favorite` |
| GET / POST | `/api/v1/filter-presets` | `_api_get_api_filter_presets`, `_api_post_api_filter_presets` | `_api_get_api_filter_presets` |
| POST | `/api/v1/game` | `_api_post_api_game` | `saved game record` |
| POST | `/api/v1/game/delete` | `_api_post_api_game_delete` | `removed game name` |
| POST | `/api/v1/games/bulk` | `_api_post_api_games_bulk` | `_api_post_api_games_bulk` |
| POST | `/api/v1/games/bulk-wizard` | `_api_post_api_games_bulk_wizard` | `_api_post_api_games_bulk_wizard` |
| POST | `/api/v1/gameyfin/test` | `_api_post_api_gameyfin_test` | `_api_post_api_gameyfin_test` |
| POST | `/api/v1/health` | `_api_post_api_health` | `health facts and job history` |
| POST | `/api/v1/health/dedupe` | `_api_post_api_health_dedupe` | `_api_post_api_health_dedupe` |
| GET | `/api/v1/history` | `_api_get_api_history` | `enabled, history` |
| POST | `/api/v1/import` | `_api_post_api_import` | `added, found, recommendations` |
| POST | `/api/v1/import/arcade` | `_api_post_api_import_arcade` | `_api_post_api_import_arcade` |
| POST | `/api/v1/import/epic` | `_api_post_api_import_epic` | `_api_post_api_import_epic` |
| POST | `/api/v1/import/heroic` | `_api_post_api_import_heroic` | `_api_post_api_import_heroic` |
| POST | `/api/v1/import/lutris` | `_api_post_api_import_lutris` | `_api_post_api_import_lutris` |
| POST | `/api/v1/import/rpcs3` | `_api_post_api_import_rpcs3` | `_api_post_api_import_rpcs3` |
| POST | `/api/v1/import/scummvm` | `_api_post_api_import_scummvm` | `_api_post_api_import_scummvm` |
| POST | `/api/v1/import/steam` | `_api_post_api_import_steam` | `_api_post_api_import_steam` |
| POST | `/api/v1/import/vita3k` | `_api_post_api_import_vita3k` | `_api_post_api_import_vita3k` |
| GET | `/api/v1/jobs` | `_api_get_api_jobs` | `jobs, history` |
| POST | `/api/v1/launch` | `_api_post_api_launch` | `ok, launch_id, game` |
| GET | `/api/v1/library` | `_api_get_api_library` | `games, playlists, settings, platforms, categories, tags, queue` |
| GET | `/api/v1/log` | `_api_get_api_log` | `_api_get_api_log` |
| GET | `/api/v1/media` | `_api_get_api_media` | `media bytes or a JSON error` |
| GET | `/api/v1/media/audit` | `_api_get_api_media_audit` | `_api_get_api_media_audit` |
| POST | `/api/v1/media/bulk` | `_api_post_api_media_bulk` | `media job state` |
| POST | `/api/v1/media/cleanup` | `_api_post_api_media_cleanup` | `_api_post_api_media_cleanup` |
| POST | `/api/v1/metadata/apply` | `_api_post_api_metadata_apply` | `applied field names and notes` |
| POST | `/api/v1/metadata/match` | `_api_post_api_metadata_match` | `_api_post_api_metadata_match` |
| GET | `/api/v1/metadata/search` | `_api_get_api_metadata_search` | `_api_get_api_metadata_search` |
| GET | `/api/v1/metadata/status` | `_api_get_api_metadata_status` | `ready, coverage, matched counts` |
| GET / POST | `/api/v1/notifications` | `_api_get_api_notifications`, `_api_post_api_notifications` | `notifications and unread count` |
| POST | `/api/v1/playlists` | `_api_post_api_playlists` | `saved playlist name` |
| GET | `/api/v1/plugins` | `_api_get_api_plugins` | `_api_get_api_plugins` |
| GET | `/api/v1/premium/media-packs` | `_api_get_api_premium_media_packs` | `_api_get_api_premium_media_packs` |
| POST | `/api/v1/premium/media-packs/apply` | `_api_post_api_premium_media_packs_apply` | `_api_post_api_premium_media_packs_apply` |
| GET / POST | `/api/v1/profiles` | `_api_get_api_profiles`, `_api_post_api_profiles` | `emulator profile map` |
| GET / POST | `/api/v1/queue` | `_api_get_api_queue`, `_api_post_api_queue` | `queue and the advanced game` |
| POST | `/api/v1/ra/inject` | `_api_post_api_ra_inject` | `_api_post_api_ra_inject` |
| GET | `/api/v1/running` | `_api_get_api_running` | `running, events, last_event` |
| GET | `/api/v1/saves` | `_api_get_api_saves` | `discovered save backups` |
| POST | `/api/v1/saves/scan/apply` | `_api_post_api_saves_scan_apply` | `_api_post_api_saves_scan_apply` |
| GET / POST | `/api/v1/settings` | `_api_get_api_settings`, `_api_post_api_settings` | `the full public settings object` |
| POST | `/api/v1/shutdown` | `_api_post_api_shutdown` | `_api_post_api_shutdown` |
| POST | `/api/v1/state/recover` | `_api_post_api_state_recover` | `_api_post_api_state_recover` |
| POST | `/api/v1/storefront/import` | `_api_post_api_storefront_import` | `_api_post_api_storefront_import` |
| GET / POST | `/api/v1/tags` | `_api_get_api_tags`, `_api_post_api_tags` | `updated count and tag counts` |
| GET | `/api/v1/themes` | `_api_get_api_themes` | `installed themes and the active theme` |
| POST | `/api/v1/themes/open-folder` | `_api_post_api_themes_open_folder` | `_api_post_api_themes_open_folder` |
| GET | `/api/v1/update` | `_api_get_api_update` | `available, latest, current` |
| POST | `/api/v1/update/install` | `_api_post_api_update_install` | `_api_post_api_update_install` |
| GET / POST | `/api/v1/webhooks` | `_api_get_api_webhooks`, `_api_post_api_webhooks` | `saved webhooks and event types` |

_61 routes._

