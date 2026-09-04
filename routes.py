"""Route tables mapping HTTP paths to Handler method names."""

from pathlib import Path

from api_errors import RouteNotFound

__path__ = [str(Path(__file__).resolve().parent / "routes")]

PUBLIC_GET_PATHS = frozenset({
    "/",
    "/index.html",
    "/static/app.js",
    "/static/util.js",
    "/static/state.js",
    "/static/library.js",
    "/static/settings.js",
    "/static/imports.js",
    "/static/metadata.js",
    "/static/media.js",
    "/static/reader.js",
    "/static/sessions.js",
    "/static/bigbox.js",
    "/static/storefront.js",
    "/static/dialogs.js",
    "/static/setup.js",
    "/static/activity.js",
    "/static/i18n.js",
    "/static/mood.js",
    "/static/picker.js",
    "/static/constellation.js",
    "/static/wrapped.js",
    "/static/timeline.js",
    "/static/mastery.js",
    "/static/party.js",
    "/static/app.css",
    "/static/logo.png",
    "/locales/en.json",
    "/locales/es.json",
    "/locales/de.json",
    "/locales/fr.json",
    "/locales/pt.json",
    "/favicon.ico",
    "/favicon.svg",
})

GET_TABLE = {
    "/": "_api_get_index",
    "/api/backup": "_api_get_api_backup",
    "/api/backup/manifest": "_api_get_api_backup_manifest",
    "/api/backups": "_api_get_api_backups",
    "/api/diagnostic": "_api_get_api_diagnostic",
    "/api/discovery": "_api_get_api_discovery",
    "/api/document": "_api_get_api_document",
    "/api/emulators": "_api_get_api_emulators",
    "/api/emulators/definitions": "_api_get_api_emulators_definitions",
    "/api/emulators/dependencies": "_api_get_api_emulators_dependencies",
    "/api/emulators/recommend": "_api_get_api_emulators_recommend",
    "/api/emulators/scan-configs": "_api_get_api_emulators_scan_configs",
    "/api/v2/emulators/registry": "_api_get_api_v2_emulators_registry",
    "/api/explorer/facets": "_api_get_api_explorer_facets",
    "/api/filter-presets": "_api_get_api_filter_presets",
    "/api/gameyfin/install/status": "_api_get_api_gameyfin_install_status",
    "/api/gameyfin/providers": "_api_get_api_gameyfin_providers",
    "/api/highscores": "_api_get_api_highscores",
    "/api/history": "_api_get_api_history",
    "/api/jobs": "_api_get_api_jobs",
    "/api/v2/jobs": "_api_get_api_v2_jobs",
    "/api/v2/jobs/items": "_api_get_api_v2_jobs_items",
    "/api/v2/setup/summary": "_api_get_api_v2_setup_summary",
    "/api/v2/setup/preview": "_api_get_api_v2_setup_preview",
    "/api/v2/setup/preview/items": "_api_get_api_v2_setup_preview_items",
    "/api/v2/metadata/matches/preview": "_api_get_api_v2_metadata_matches_preview",
    "/api/v2/metadata/matches/items": "_api_get_api_v2_metadata_matches_items",
    "/api/v2/insights/summary": "_api_get_api_v2_insights_summary",
    "/api/v2/insights/heatmap": "_api_get_api_v2_insights_heatmap",
    "/api/v2/insights/wrapped": "_api_get_api_v2_insights_wrapped",
    "/api/v2/insights/mastery": "_api_get_api_v2_insights_mastery",
    "/api/v2/history/timeline": "_api_get_api_v2_history_timeline",
    "/api/v2/library/constellation": "_api_get_api_v2_library_constellation",
    "/api/v2/library/search": "_api_get_api_v2_library_search",
    "/api/v2/party/queue": "_api_get_api_v2_party_queue",
    "/api/v2/backup/diff": "_api_get_api_v2_backup_diff",
    "/api/v2/screenscraper/search": "_api_get_api_v2_screenscraper_search",
    "/api/v2/screenscraper/status": "_api_get_api_v2_screenscraper_status",
    "/api/v2/library/export/exports": "_api_get_api_v2_library_export_exports",
    "/api/v2/library/export/download": "_api_get_api_v2_library_export_download",
    "/static/app.js": "_api_get_static",
    "/static/util.js": "_api_get_static",
    "/static/state.js": "_api_get_static",
    "/static/library.js": "_api_get_static",
    "/static/settings.js": "_api_get_static",
    "/static/imports.js": "_api_get_static",
    "/static/metadata.js": "_api_get_static",
    "/static/media.js": "_api_get_static",
    "/static/reader.js": "_api_get_static",
    "/static/sessions.js": "_api_get_static",
    "/static/bigbox.js": "_api_get_static",
    "/static/storefront.js": "_api_get_static",
    "/static/dialogs.js": "_api_get_static",
    "/static/setup.js": "_api_get_static",
    "/static/activity.js": "_api_get_static",
    "/static/insights.js": "_api_get_static",
    "/static/mood.js": "_api_get_static",
    "/static/picker.js": "_api_get_static",
    "/static/constellation.js": "_api_get_static",
    "/static/wrapped.js": "_api_get_static",
    "/static/timeline.js": "_api_get_static",
    "/static/mastery.js": "_api_get_static",
    "/static/party.js": "_api_get_static",
    "/static/worker.search.js": "_api_get_static",
    "/static/i18n.js": "_api_get_static",
    "/static/app.css": "_api_get_static",
    "/static/logo.png": "_api_get_static",
    "/locales/en.json": "_api_get_locale",
    "/locales/es.json": "_api_get_locale",
    "/locales/de.json": "_api_get_locale",
    "/locales/fr.json": "_api_get_locale",
    "/locales/pt.json": "_api_get_locale",
    "/api/import/exclusions": "_api_get_api_import_exclusions",
    "/api/launcher/menu": "_api_get_api_launcher_menu",
    "/api/library": "_api_get_api_library",
    "/api/library/delta": "_api_get_api_library_delta",
    "/api/log": "_api_get_api_log",
    "/api/media": "_api_get_api_media",
    "/api/media/audit": "_api_get_api_media_audit",
    "/api/media/bulk/status": "_api_get_api_media_bulk_status",
    "/api/media/duplicates": "_api_get_api_media_duplicates",
    "/api/media/queue": "_api_get_api_media_queue",
    "/api/metadata/igdb/search": "_api_get_api_metadata_igdb_search",
    "/api/metadata/search": "_api_get_api_metadata_search",
    "/api/metadata/status": "_api_get_api_metadata_status",
    "/api/notifications": "_api_get_api_notifications",
    "/api/obs/status": "_api_get_api_obs_status",
    "/api/perf_profiles": "_api_get_api_perf_profiles",
    "/api/platform/document": "_api_get_api_platform_document",
    "/api/platform/documents": "_api_get_api_platform_documents",
    "/api/plugins": "_api_get_api_plugins",
    "/api/plugins/catalog": "_api_get_api_plugins_catalog",
    "/api/premium/media-packs": "_api_get_api_premium_media_packs",
    "/api/premium/platform-categories": "_api_get_api_premium_platform_categories",
    "/api/premium/strings": "_api_get_api_premium_strings",
    "/api/profiles": "_api_get_api_profiles",
    "/api/queue": "_api_get_api_queue",
    "/api/ra/badge": "_api_get_api_ra_badge",
    "/api/ra/settings": "_api_get_api_ra_settings",
    "/api/related": "_api_get_api_related",
    "/api/related/rich": "_api_get_api_related_rich",
    "/api/running": "_api_get_api_running",
    "/api/save-tools/status": "_api_get_api_save_tools_status",
    "/api/saves": "_api_get_api_saves",
    "/api/saves/discover": "_api_get_api_saves_discover",
    "/api/saves/scan": "_api_get_api_saves_scan",
    "/api/settings": "_api_get_api_settings",
    "/api/storefront/catalog": "_api_get_api_storefront_catalog",
    "/api/tags": "_api_get_api_tags",
    "/api/theme.css": "_api_get_api_theme_css",
    "/api/themes": "_api_get_api_themes",
    "/api/update": "_api_get_api_update",
    "/api/webhooks": "_api_get_api_webhooks",
    "/api/events": "_api_get_api_events",
    "/api/wine/prefixes": "_api_get_api_wine_prefixes",
    "/api/wine/protons": "_api_get_api_wine_protons",
    "/api/wine/prefix-for-game": "_api_get_api_wine_prefix_for_game",
    "/api/faugus/status": "_api_get_api_faugus_status",
    "/api/faugus/scan": "_api_get_api_faugus_scan",
    "/api/native/capabilities": "handlers.native.capabilities",
    "/favicon.ico": "_api_get_favicon",
    "/favicon.svg": "_api_get_favicon",
    "/index.html": "_api_get_index",
}

POST_TABLE = {
    "/api/backup/create": "_api_post_api_backup_create",
    "/api/backup/restore": "_api_post_api_backup_restore",
    "/api/bezels/download": "_api_post_api_bezels_download",
    "/api/bigbox/mode": "_api_post_api_bigbox_mode",
    "/api/cloud/sync": "_api_post_api_cloud_sync",
    "/api/desktop/install": "_api_post_api_desktop_install",
    "/api/emulators/install": "_api_post_api_emulators_install",
    "/api/emulators/install-all": "_api_post_api_emulators_install_all",
    "/api/emulators/open": "_api_post_api_emulators_open",
    "/api/emulators/scan": "_api_post_api_emulators_scan",
    "/api/emulators/scan-configs": "_api_post_api_emulators_scan_configs",
    "/api/emulators/update": "_api_post_api_emulators_update",
    "/api/emulators/update-all": "_api_post_api_emulators_update_all",
    "/api/emumovies/download": "_api_post_api_emumovies_download",
    "/api/emumovies/settings": "_api_post_api_emumovies_settings",
    "/api/extra/launch": "_api_post_api_extra_launch",
    "/api/favorite": "_api_post_api_favorite",
    "/api/filter-presets": "_api_post_api_filter_presets",
    "/api/filter-presets/delete": "_api_post_api_filter_presets_delete",
    "/api/game": "_api_post_api_game",
    "/api/game/delete": "_api_post_api_game_delete",
    "/api/games/bulk": "_api_post_api_games_bulk",
    "/api/games/bulk-wizard": "_api_post_api_games_bulk_wizard",
    "/api/games/delete-steam": "_api_post_api_games_delete_steam",
    "/api/gameyfin/install": "_api_post_api_gameyfin_install",
    "/api/gameyfin/test": "_api_post_api_gameyfin_test",
    "/api/gameyfin/uninstall": "_api_post_api_gameyfin_uninstall",
    "/api/health": "_api_post_api_health",
    "/api/health/dedupe": "_api_post_api_health_dedupe",
    "/api/highscores/export": "_api_post_api_highscores_export",
    "/api/highscores/import": "_api_post_api_highscores_import",
    "/api/image-group": "_api_post_api_image_group",
    "/api/import": "_api_post_api_import",
    "/api/import/arcade": "_api_post_api_import_arcade",
    "/api/import/exclusions": "_api_post_api_import_exclusions",
    "/api/import/exclusions/delete": "_api_post_api_import_exclusions_delete",
    "/api/import/heroic": "_api_post_api_import_heroic",
    "/api/import/loose-arcade": "_api_post_api_import_loose_arcade",
    "/api/import/lutris": "_api_post_api_import_lutris",
    "/api/import/rpcs3": "_api_post_api_import_rpcs3",
    "/api/import/scummvm": "_api_post_api_import_scummvm",
    "/api/import/steam": "_api_post_api_import_steam",
    "/api/import/vita3k": "_api_post_api_import_vita3k",
    "/api/import/watch": "_api_post_api_import_watch",
    "/api/import/wizard": "_api_post_api_import_wizard",
    "/api/import/xbox360": "_api_post_api_import_xbox360",
    "/api/launch": "_api_post_api_launch",
    "/api/v2/launch/preflight": "_api_post_api_v2_launch_preflight",
    "/api/v2/launch/preflight/batch": "_api_post_api_v2_launch_preflight_batch",
    "/api/v2/jobs/cancel": "_api_post_api_v2_jobs_cancel",
    "/api/v2/jobs/retry": "_api_post_api_v2_jobs_retry",
    "/api/v2/jobs/resume": "_api_post_api_v2_jobs_resume",
    "/api/v2/library/export": "_api_post_api_v2_library_export",
    "/api/v2/library/pick": "_api_post_api_v2_library_pick",
    "/api/v2/import/launchbox/preview": "_api_post_api_v2_import_launchbox_preview",
    "/api/v2/import/launchbox/apply": "_api_post_api_v2_import_launchbox_apply",
    "/api/v2/library/sync/publish": "_api_post_api_v2_library_sync_publish",
    "/api/v2/library/sync/pull": "_api_post_api_v2_library_sync_pull",
    "/api/v2/library/manual-entry": "_api_post_api_v2_library_manual_entry",
    "/api/v2/party/queue": "_api_post_api_v2_party_queue",
    "/api/v2/party/next": "_api_post_api_v2_party_next",
    "/api/v2/screenscraper/apply": "_api_post_api_v2_screenscraper_apply",
    "/api/v2/screenscraper/info": "_api_post_api_v2_screenscraper_info",
    "/api/v2/screenscraper/match": "_api_post_api_v2_screenscraper_match",
    "/api/v2/screenscraper/test": "_api_post_api_v2_screenscraper_test",
    "/api/v2/setup/preview": "_api_post_api_v2_setup_preview",
    "/api/v2/setup/preview/decisions": "_api_post_api_v2_setup_preview_decisions",
    "/api/v2/setup/preview/revalidate": "_api_post_api_v2_setup_preview_revalidate",
    "/api/v2/setup/commit": "_api_post_api_v2_setup_commit",
    "/api/v2/metadata/matches/preview": "_api_post_api_v2_metadata_matches_preview",
    "/api/v2/metadata/matches/decisions": "_api_post_api_v2_metadata_matches_decisions",
    "/api/v2/metadata/matches/apply": "_api_post_api_v2_metadata_matches_apply",
    "/api/media/bulk": "_api_post_api_media_bulk",
    "/api/media/cleanup": "_api_post_api_media_cleanup",
    "/api/metadata/apply": "_api_post_api_metadata_apply",
    "/api/metadata/gog": "_api_post_api_metadata_gog",
    "/api/metadata/igdb/apply": "_api_post_api_metadata_igdb_apply",
    "/api/metadata/steam": "_api_post_api_metadata_steam",
    "/api/metadata/sync": "_api_post_api_metadata_sync",
    "/api/metadata/match": "_api_post_api_metadata_match",
    "/api/metadata/trailer": "_api_post_api_metadata_trailer",
    "/api/notifications": "_api_post_api_notifications",
    "/api/obs/attach": "_api_post_api_obs_attach",
    "/api/perf_profiles": "_api_post_api_perf_profiles",
    "/api/platform/documents": "_api_post_api_platform_documents",
    "/api/playlists": "_api_post_api_playlists",
    "/api/playlists/delete": "_api_post_api_playlists_delete",
    "/api/plugins/catalog/install": "_api_post_api_plugins_catalog_install",
    "/api/plugins/install": "_api_post_api_plugins_install",
    "/api/plugins/remove": "_api_post_api_plugins_remove",
    "/api/plugins/toggle": "_api_post_api_plugins_toggle",
    "/api/premium/media-packs/apply": "_api_post_api_premium_media_packs_apply",
    "/api/profiles": "_api_post_api_profiles",
    "/api/queue": "_api_post_api_queue",
    "/api/ra/game": "_api_post_api_ra_game",
    "/api/ra/inject": "_api_post_api_ra_inject",
    "/api/ra/settings": "_api_post_api_ra_settings",
    "/api/save-tools/hoard": "_api_post_api_save_tools_hoard",
    "/api/save-tools/ludusavi": "_api_post_api_save_tools_ludusavi",
    "/api/saves/add": "_api_post_api_saves_add",
    "/api/saves/backup": "_api_post_api_saves_backup",
    "/api/saves/restore": "_api_post_api_saves_restore",
    "/api/saves/scan/apply": "_api_post_api_saves_scan_apply",
    "/api/screenshot": "_api_post_api_screenshot",
    "/api/session/control": "_api_post_api_session_control",
    "/api/session/cleanup": "_api_post_api_session_cleanup",
    "/api/settings": "_api_post_api_settings",
    "/api/faugus/import": "_api_post_api_faugus_import",
    "/api/shutdown": "_api_post_api_shutdown",
    "/api/state/recover": "_api_post_api_state_recover",
    "/api/storefront/import": "_api_post_api_storefront_import",
    "/api/tags": "_api_post_api_tags",
    "/api/themes/import": "_api_post_api_themes_import",
    "/api/themes/open-folder": "_api_post_api_themes_open_folder",
    "/api/themes/select": "_api_post_api_themes_select",
    "/api/update/install": "_api_post_api_update_install",
    "/api/native/dialog": "handlers.native.dialog",
    "/api/native/open-external": "handlers.native.open_external",
    "/api/native/reveal": "handlers.native.reveal",
    "/api/native/window": "handlers.native.window",
    "/api/webhooks": "_api_post_api_webhooks",
    "/api/webhooks/test": "_api_post_api_webhooks_test",
}


# Paths also served under /api/v1/<path> for legacy clients.
V1_ALIASED_PREFIXES = (
    "/api/library",
    "/api/settings",
    "/api/health",
    "/api/health/dedupe",
    "/api/launch",
    "/api/game",
    "/api/game/delete",
    "/api/games/bulk",
    "/api/queue",
    "/api/tags",
    "/api/notifications",
    "/api/webhooks",
    "/api/playlists",
    "/api/running",
    "/api/history",
    "/api/saves",
    "/api/media",
    "/api/media/bulk",
    "/api/media/audit",
    "/api/metadata/status",
    "/api/metadata/apply",
    "/api/metadata/match",
    "/api/metadata/search",
    "/api/import",
    "/api/import/steam",
    "/api/import/heroic",
    "/api/import/lutris",
    "/api/import/arcade",
    "/api/emulators",
    "/api/emulators/install",
    "/api/profiles",
    "/api/themes",
    "/api/update",
    "/api/update/install",
    "/api/backup",
    "/api/backup/create",
    "/api/backup/restore",
    "/api/backups",
    "/api/jobs",
    "/api/log",
    "/api/diagnostic",
    "/api/shutdown",
    "/api/favorite",
    "/api/plugins",
    "/api/state/recover",
    "/api/filter-presets",
    "/api/premium/media-packs",
    "/api/premium/media-packs/apply",
    "/api/storefront/import",
    "/api/gameyfin/test",
    "/api/import/scummvm",
    "/api/import/rpcs3",
    "/api/import/vita3k",
    "/api/themes/open-folder",
    "/api/ra/inject",
    "/api/media/cleanup",
    "/api/saves/scan/apply",
    "/api/bigbox/mode",
    "/api/games/bulk-wizard",
    "/api/extra/launch",
)


def _apply_v1_aliases(table, dispatcher_name):
    for path in V1_ALIASED_PREFIXES:
        if path in table:
            table[f"/api/v1{path[len('/api'):]}"] = table[path]
    return table


def _build_tables():
    # Base tables plus v1 aliases only; @route registry is consulted at dispatch time
    # so decorator-only routes do not need a build-time merge (which would run
    # before handlers are imported and appear empty).
    get_tbl = _apply_v1_aliases(dict(GET_TABLE), "GET")
    post_tbl = _apply_v1_aliases(dict(POST_TABLE), "POST")
    return get_tbl, post_tbl


GET_TABLE, POST_TABLE = _build_tables()

def _resolve(spec):
    """Resolve a route entry: a bare name is a Handler method, a dotted name a handlers-package function."""
    if "." not in spec:
        return None  # caller falls back to getattr(handler, spec)
    import importlib
    module_name, _, attr = spec.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _is_static_asset(path):
    if not path.startswith("/static/"):
        return False
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".js") or name in {"app.css", "logo.png"}


def _is_public_path(path):
    if path in PUBLIC_GET_PATHS:
        return True
    # Allow any future ES-module chunk under /static/ without requiring a
    # route table update. The handler still validates the file exists on disk.
    if _is_static_asset(path):
        return True
    return False


def _lookup_registry(method, path):
    """Check the decorator registry for a path not in the static tables."""
    try:
        from routes.registry import _REGISTRY

        entry = _REGISTRY.get((method, path))
        if entry is not None:
            return entry.spec
        # v1 alias fallback for registry entries
        if path.startswith("/api/v1/"):
            base = "/api" + path[len("/api/v1"):]
            entry = _REGISTRY.get((method, base))
            if entry is not None and base in V1_ALIASED_PREFIXES:
                return entry.spec
    except Exception:
        pass
    return None


def dispatch_get(handler, parsed):
    spec = GET_TABLE.get(parsed.path)
    if spec is None:
        spec = _lookup_registry("GET", parsed.path)
    if spec is None:
        if _is_static_asset(parsed.path):
            spec = "_api_get_static"
        else:
            raise RouteNotFound("Not found")
    if not _is_public_path(parsed.path) and not handler.authorized():
        handler.handle_unauthorized()
        return
    callable_ = _resolve(spec)
    if callable_ is None:
        getattr(handler, spec)(parsed)
    else:
        callable_(handler, parsed)

def dispatch_post(handler, route, payload):
    spec = POST_TABLE.get(route)
    if spec is None:
        spec = _lookup_registry("POST", route)
    if spec is None:
        raise RouteNotFound("Not found")
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    callable_ = _resolve(spec)
    if callable_ is None:
        getattr(handler, spec)(payload)
    else:
        callable_(handler, payload)
