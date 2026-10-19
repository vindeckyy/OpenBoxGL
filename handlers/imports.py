"""ImportsHandlers capability handlers. Import sources, storefront catalogs, and import exclusions."""

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadRequest
from arcade import import_arcade
from emulators import install_emulator
from routes.registry import route
from importers import import_epic, import_heroic, import_lutris, import_steam
from openbox import DATA, load_state
from parity_import import import_rpcs3_hdd, import_scummvm, import_vita3k
from parity_import_policy import add_exclusion, list_exclusions, remove_exclusion
from pkg.parity.parity_legacy_import import import_msu1, import_pico8, import_whdload
from pkg.parity.parity_plugin_storefront import list_storefront_plugins, plugin_storefront_catalog, plugin_storefront_import
from parity_premium import import_loose_arcade, import_xbox360_folder
from parity_storefront import catalog_entries_to_games, storefront_catalog
from pkg.parity.parity_storefront_windows import (
    import_ea as import_ea_windows,
    import_gog as import_gog_windows,
    import_ubisoft as import_ubisoft_windows,
)
from webapp_state import (
    JOB_MANAGER,
    broadcast_event,
    clear_file_probe_cache,
    import_folder_path,
    load_state_view,
    merge_imported_games,
    transact_state,
)
from pkg.state.imports import (
    read_storefront_status,
    storefront_scan_worker,
    storefront_source_ids,
)

LOGGER = logging.getLogger("openbox")

SUPPORTED_STOREFRONT_IMPORT_SOURCES = {"steam", "heroic", "lutris", "gameyfin"}

def _required_folder_path(payload):
    value = payload.get("folder", "")
    if value is None:
        raise BadRequest("Folder path is required.")
    folder = str(value)
    if not folder.strip():
        raise BadRequest("Folder path is required.")
    return folder
def _send_import_result(handler, added, found, **extra):
    clear_file_probe_cache()
    payload = {"added": added, "found": found}
    payload.update(extra)
    handler.send_json(200, payload)


class ImportsHandlers:
    @route("GET", "/api/v2/storefronts")
    def _api_get_api_v2_storefronts(self, parsed):
        """Built-in and plugin storefronts with redacted plugin settings."""
        builtins = [
            {"id": source, "name": source.title(), "kind": "builtin", "installed": True}
            for source in ("steam", "heroic", "lutris", "gameyfin", "gog", "ea", "ubisoft")
        ]
        plugins = [
            {**item, "kind": "plugin", "installed": None}
            for item in list_storefront_plugins(DATA.parent / "plugins")
        ]
        self.send_json(200, {"storefronts": builtins + plugins})
        return

    @route("POST", "/api/v2/storefronts/plugin/catalog")
    def _api_post_api_v2_storefronts_plugin_catalog(self, payload):
        plugin_id = str(payload.get("plugin_id") or "").strip()
        entries = plugin_storefront_catalog(
            plugin_id,
            installed_only=bool(payload.get("installed_only")),
            uninstalled_only=bool(payload.get("uninstalled_only")),
            plugins_dir=DATA.parent / "plugins",
        )
        self.send_json(200, {"plugin_id": plugin_id, "entries": entries[:2000]})
        return

    @route("POST", "/api/v2/storefronts/plugin/import")
    def _api_post_api_v2_storefronts_plugin_import(self, payload):
        plugin_id = str(payload.get("plugin_id") or "").strip()
        imported = plugin_storefront_import(
            plugin_id,
            installed_only=bool(payload.get("installed_only")),
            uninstalled_only=bool(payload.get("uninstalled_only")),
            plugins_dir=DATA.parent / "plugins",
        )
        def identity(game):
            return ("plugin", plugin_id, str(game.get("game_id", "")))

        added, found = merge_imported_games(imported, identity)
        _send_import_result(self, added, found, imported=len(imported), plugin_id=plugin_id)
        return

    @route("GET", "/api/v2/storefront/status")
    def _api_get_api_v2_storefront_status(self, parsed):
        self.send_json(200, read_storefront_status())
        return

    @route("POST", "/api/v2/storefront/scan")
    def _api_post_api_v2_storefront_scan(self, payload):
        known = storefront_source_ids(DATA.parent / "plugins")
        raw_sources = payload.get("sources")
        if raw_sources is None:
            sources = list(known)
        elif isinstance(raw_sources, list) and len(raw_sources) <= 20:
            sources = []
            for value in raw_sources:
                source = str(value or "").strip().casefold()
                if source not in known:
                    raise BadRequest(f"Unknown storefront source: {source or '(blank)'}")
                if source not in sources:
                    sources.append(source)
        else:
            raise BadRequest("sources must be a list of storefront ids.")
        include_uninstalled = bool(payload.get("include_uninstalled", True))
        if not sources:
            raise BadRequest("At least one storefront source is required.")

        def worker(cancel_event):
            return storefront_scan_worker(
                cancel_event, sources=sources, include_uninstalled=include_uninstalled
            )

        job = JOB_MANAGER.submit("storefront-scan", worker, replace=True)
        self.send_json(202, {"job_id": job["job_id"], "sources": sources})
        return

    @route("GET", "/api/storefront/catalog")
    def _api_get_api_storefront_catalog(self, parsed):
        source = parse_qs(parsed.query).get("source", [""])[0]
        try:
            self.send_json(200, {"catalog": storefront_catalog(source, settings=load_state_view().get("settings", {}))})
        except (ValueError, OSError, FileNotFoundError, subprocess.SubprocessError) as error:
            raise BadRequest(str(error)) from None
        return
    @route("GET", "/api/import/exclusions")
    def _api_get_api_import_exclusions(self, parsed):
        self.send_json(200, {"exclusions": list_exclusions(load_state_view())})
        return

    @route("POST", "/api/import")
    def _api_post_api_import(self, payload):
        self.import_folder(payload)

    @route("POST", "/api/import/wizard")
    def _api_post_api_import_wizard(self, payload):
        self.import_wizard(payload)

    @route("POST", "/api/import/xbox360")
    def _api_post_api_import_xbox360(self, payload):
        self.import_xbox360(payload)

    @route("POST", "/api/import/loose-arcade")
    def _api_post_api_import_loose_arcade(self, payload):
        self.import_loose_arcade_route(payload)

    @route("POST", "/api/import/watch")
    def _api_post_api_import_watch(self, payload):
        self.scan_watch_folders()

    @route("POST", "/api/import/steam")
    def _api_post_api_import_steam(self, payload):
        self.import_steam_games()

    @route("POST", "/api/import/heroic")
    def _api_post_api_import_heroic(self, payload):
        self.import_heroic_games()

    @route("POST", "/api/import/lutris")
    def _api_post_api_import_lutris(self, payload):
        self.import_lutris_games()

    @route("POST", "/api/import/epic")
    def _api_post_api_import_epic(self, payload):
        self.import_epic_games()

    @route("POST", "/api/v2/import/gog")
    def _api_post_api_v2_import_gog(self, payload):
        if not isinstance(payload, dict):
            raise BadRequest("body must be an object")
        self.import_gog_games()

    @route("POST", "/api/v2/import/ea")
    def _api_post_api_v2_import_ea(self, payload):
        if not isinstance(payload, dict):
            raise BadRequest("body must be an object")
        self.import_ea_games()

    @route("POST", "/api/v2/import/ubisoft")
    def _api_post_api_v2_import_ubisoft(self, payload):
        if not isinstance(payload, dict):
            raise BadRequest("body must be an object")
        self.import_ubisoft_games()

    @route("POST", "/api/import/arcade")
    def _api_post_api_import_arcade(self, payload):
        self.import_arcade_games(payload)

    @route("POST", "/api/import/scummvm")
    def _api_post_api_import_scummvm(self, payload):
        self.import_scummvm_games()

    @route("POST", "/api/import/rpcs3")
    def _api_post_api_import_rpcs3(self, payload):
        self.import_rpcs3_games()

    @route("POST", "/api/import/vita3k")
    def _api_post_api_import_vita3k(self, payload):
        self.import_vita3k_games()

    @route("POST", "/api/v2/import/pico8")
    def _api_post_api_v2_import_pico8(self, payload):
        self.import_legacy_games(payload, import_pico8, "PICO-8")

    @route("POST", "/api/v2/import/msu1")
    def _api_post_api_v2_import_msu1(self, payload):
        self.import_legacy_games(payload, import_msu1, "MSU-1")

    @route("POST", "/api/v2/import/whdload")
    def _api_post_api_v2_import_whdload(self, payload):
        self.import_legacy_games(payload, import_whdload, "WHDLoad")

    @route("POST", "/api/storefront/import")
    def _api_post_api_storefront_import(self, payload):
        self.import_storefront_catalog(payload)

    @route("POST", "/api/import/exclusions")
    def _api_post_api_import_exclusions(self, payload):
        self.add_import_exclusion(payload)

    @route("POST", "/api/import/exclusions/delete")
    def _api_post_api_import_exclusions_delete(self, payload):
        self.remove_import_exclusion(payload)

    @route("POST", "/api/v2/import/launchbox/preview")
    def _api_post_api_v2_import_launchbox_preview(self, payload):
        from pkg.parity.parity_launchbox_import import preview_import

        xml_path = str(payload.get("xml_path", "")).strip()
        if not xml_path:
            raise BadRequest("xml_path is required.")
        if not Path(xml_path).is_file():
            raise BadRequest(f"LaunchBox XML not found: {xml_path}")
        state = load_state_view()
        report = preview_import(xml_path, state, options=payload.get("options") or {})
        self.send_json(200, report)

    @route("POST", "/api/v2/import/launchbox/apply")
    def _api_post_api_v2_import_launchbox_apply(self, payload):
        from pkg.parity.parity_launchbox_import import (
            StaleImportPlan,
            apply_import_plan,
            build_import_plan,
            parse_launchbox_xml,
        )

        xml_path = str(payload.get("xml_path", "")).strip()
        if not xml_path:
            raise BadRequest("xml_path is required.")
        if not Path(xml_path).is_file():
            raise BadRequest(f"LaunchBox XML not found: {xml_path}")
        supplied_plan = payload.get("plan")
        options = payload.get("options") if "options" in payload else (
            supplied_plan.get("options") if isinstance(supplied_plan, dict) else {}
        )
        options = options or {}
        if supplied_plan:
            parsed = parse_launchbox_xml(xml_path)
            if str(parsed.get("source_digest")) != str(supplied_plan.get("source_digest")):
                raise BadRequest("LaunchBox XML changed since the preview; review it again.")

            def mutate(current):
                result_state = apply_import_plan(
                    supplied_plan,
                    current,
                    preview_token=payload.get("preview_token") or supplied_plan.get("preview_token"),
                    source_digest=parsed.get("source_digest"),
                    source=parsed,
                    options=options,
                )
                current["games"] = result_state["games"]
                current["import_counts"] = result_state.get("import_counts", {})
                return result_state.get("import_counts", {})

            try:
                counts = transact_state(mutate)[1]
            except StaleImportPlan as error:
                raise BadRequest(str(error)) from None
            result = counts
        else:
            parsed = parse_launchbox_xml(xml_path)

            def mutate(current):
                canonical = build_import_plan(parsed, current, options=options)
                result_state = apply_import_plan(
                    canonical, current, preview_token=canonical["preview_token"],
                    source_digest=parsed.get("source_digest"), source=parsed, options=options,
                )
                current["games"] = result_state["games"]
                current["import_counts"] = result_state.get("import_counts", {})
                return result_state.get("import_counts", {})

            try:
                result = transact_state(mutate)[1]
            except StaleImportPlan as error:
                raise BadRequest(str(error)) from None
        clear_file_probe_cache()
        broadcast_event("library.imported", {"source": "launchbox", "added": result.get("added", 0)})
        self.send_json(200, result)

    @route("POST", "/api/v2/import/esde/preview")
    def _api_post_api_v2_import_esde_preview(self, payload):
        from pkg.parity.parity_esde_import import ESDEImportError, build_import_plan, parse_gamelist

        xml_path = str(payload.get("xml_path", payload.get("gamelist", ""))).strip()
        if not xml_path:
            raise BadRequest("xml_path is required.")
        if not Path(xml_path).is_file():
            raise BadRequest(f"ES-DE gamelist not found: {xml_path}")
        try:
            parsed = parse_gamelist(xml_path)
            plan = build_import_plan(parsed, load_state_view(), options=payload.get("options") or {})
        except ESDEImportError as error:
            raise BadRequest(str(error), code="ESDE_INVALID_SOURCE") from error
        self.send_json(200, plan)

    @route("POST", "/api/v2/import/esde/apply")
    def _api_post_api_v2_import_esde_apply(self, payload):
        from pkg.parity.parity_esde_import import (
            ESDEImportError,
            StaleESDEPlan,
            apply_import_plan,
            build_import_plan,
            parse_gamelist,
        )

        xml_path = str(payload.get("xml_path", payload.get("gamelist", ""))).strip()
        if not xml_path:
            raise BadRequest("xml_path is required.")
        if not Path(xml_path).is_file():
            raise BadRequest(f"ES-DE gamelist not found: {xml_path}")
        options = payload.get("options") if "options" in payload else (
            payload.get("plan", {}).get("options") if isinstance(payload.get("plan"), dict) else {}
        )
        options = options or {}
        try:
            parsed = parse_gamelist(xml_path)
        except ESDEImportError as error:
            raise BadRequest(str(error), code="ESDE_INVALID_SOURCE") from error
        submitted = payload.get("plan")
        try:
            if submitted:
                if str(submitted.get("source_digest")) != str(parsed.get("source_digest")):
                    raise StaleESDEPlan("ES-DE gamelist changed since the preview; review it again.")

                def mutate(current):
                    result = apply_import_plan(
                        submitted, current,
                        preview_token=payload.get("preview_token") or submitted.get("preview_token"),
                        source_digest=parsed.get("source_digest"), source=parsed, options=options,
                    )
                    current["games"] = result["games"]
                    counts = result.get("counts", result.get("import_counts", {}))
                    current["import_counts"] = counts
                    return counts
            else:
                def mutate(current):
                    plan = build_import_plan(parsed, current, options=options)
                    result = apply_import_plan(
                        plan, current, preview_token=plan["preview_token"],
                        source_digest=parsed.get("source_digest"), source=parsed, options=options,
                    )
                    current["games"] = result["games"]
                    counts = result.get("counts", result.get("import_counts", {}))
                    current["import_counts"] = counts
                    return counts
            result = transact_state(mutate)[1]
        except (ESDEImportError, StaleESDEPlan, ValueError) as error:
            raise BadRequest(str(error), code="ESDE_STALE_PLAN" if isinstance(error, StaleESDEPlan) else "ESDE_INVALID_PLAN") from error
        clear_file_probe_cache()
        broadcast_event("library.imported", {"source": "esde", "added": result.get("added", 0)})
        self.send_json(200, result)

    def import_folder(self, payload):
        folder = _required_folder_path(payload)
        broadcast_event("job.progress", {"job": "import", "folder": folder, "state": "running"})
        added, found, recommendations = import_folder_path(
            folder,
            chosen_emulators=payload.get("chosen_emulators"),
        )
        broadcast_event("job.progress", {"job": "import", "folder": folder, "added": added, "found": found, "state": "done"})
        _send_import_result(self, added, found, recommendations=recommendations)

    def import_wizard(self, payload):
        folder = _required_folder_path(payload)
        chosen = payload.get("chosen_emulators", {})
        if not isinstance(chosen, dict):
            raise BadRequest("chosen_emulators must be an object.")
        added, found, recommendations = import_folder_path(folder, chosen_emulators=chosen)
        installs = []
        profiles = {}
        managed = set()
        canonical_ids = {"org.mamedev.MAME": "mame", "org.libretro.RetroArch": "retroarch"}
        for app_id in chosen.values():
            if not app_id:
                continue
            try:
                installed_profiles = install_emulator(str(app_id)) or {}
                profiles.update(installed_profiles)
                canonical = canonical_ids.get(str(app_id))
                if canonical:
                    managed.add(canonical)
                installs.append(str(app_id))
            except (OSError, ValueError, RuntimeError) as e:
                LOGGER.warning("install_emulator %s: %s", app_id, e)
        if profiles or managed:
            def mutate(state):
                for platform, command in profiles.items():
                    state["profiles"].setdefault(platform, command)
                for emulator_id in managed:
                    state.setdefault("emulator_settings", {}).setdefault(emulator_id, {})["managed"] = True
            transact_state(mutate)
        _send_import_result(self, added, found, recommendations=recommendations, installed=installs)

    def import_xbox360(self, payload):
        folder = _required_folder_path(payload)
        imported = import_xbox360_folder(folder, str(payload.get("command", "")))
        added, found = merge_imported_games(imported, lambda game: ("path", game.get("path", "")))
        _send_import_result(self, added, found)

    def import_loose_arcade_route(self, payload):
        folder = _required_folder_path(payload)
        imported = import_loose_arcade(folder, str(payload.get("command", "")))
        added, found = merge_imported_games(imported, lambda game: ("path", game.get("path", "")))
        _send_import_result(self, added, found)
    def scan_watch_folders(self):
        folders = load_state().get("settings", {}).get("watch_folders", [])
        added = found = 0
        errors = []
        for folder in folders:
            try:
                folder_added, folder_found, _ = import_folder_path(folder)
                added += folder_added
                found += folder_found
            except (OSError, ValueError) as error:
                errors.append(str(error))
        self.send_json(200, {"added": added, "found": found, "errors": errors})

    def import_steam_games(self):
        errors = []
        imported = import_steam(errors=errors)
        added, found = merge_imported_games(imported, lambda game: ("steam", str(game.get("steam_app_id", ""))))
        _send_import_result(self, added, found, errors=errors)

    def import_heroic_games(self):
        imported = import_heroic()
        added, found = merge_imported_games(
            imported,
            lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", ""))),
        )
        _send_import_result(self, added, found)

    def import_epic_games(self):
        imported = import_epic()
        added, found = merge_imported_games(
            imported,
            lambda game: ("epic", str(game.get("heroic_app_id", ""))),
        )
        _send_import_result(self, added, found)

    def _import_windows_store(self, discover, source, id_field):
        errors = []
        imported = discover(errors=errors)
        added, found = merge_imported_games(
            imported,
            lambda game: (source, str(game.get(id_field, ""))),
        )
        _send_import_result(self, added, found, errors=errors)

    def import_gog_games(self):
        self._import_windows_store(import_gog_windows, "gog", "gog_id")

    def import_ea_games(self):
        self._import_windows_store(import_ea_windows, "ea", "ea_id")

    def import_ubisoft_games(self):
        self._import_windows_store(import_ubisoft_windows, "ubisoft", "ubisoft_id")

    def import_lutris_games(self):
        imported = import_lutris()
        added, found = merge_imported_games(imported, lambda game: ("lutris", str(game.get("lutris_id", ""))))
        _send_import_result(self, added, found)
    def import_arcade_games(self, payload):
        folder = _required_folder_path(payload)
        skip_inputs = payload.get("skip_inputs") or []
        if not isinstance(skip_inputs, list) or len(skip_inputs) > 20:
            raise BadRequest("skip_inputs must be a list of input types.")
        imported = import_arcade(
            folder,
            str(payload.get("dat", "")),
            str(payload.get("command", "")),
            str(payload.get("source", "MAME")),
            players=str(payload.get("players", "any")),
            skip_chd=bool(payload.get("skip_chd")),
            skip_inputs=skip_inputs,
        )
        make_playlists = bool(payload.get("make_playlists"))
        def mutate(state):
            existing = {
                (game.get("source"), str(game.get("rom_name")))
                for game in state["games"] if game.get("rom_name")
            }
            new_games = [game for game in imported if (game.get("source"), game.get("rom_name")) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            if make_playlists:
                playlists = state.setdefault("playlists", [])
                names = {str(item.get("name")) for item in playlists if isinstance(item, dict)}
                for name, rules in (
                    ("Vertical", {"vertical": True}),
                    ("Requires CHD", {"requires_chd": True}),
                ):
                    if name not in names:
                        playlists.append({"name": name, "type": "filter", "rules": rules})
            return len(new_games)
        _, added = transact_state(mutate)
        clear_file_probe_cache()
        counts = {kind: sum(game["set_type"] == kind for game in imported) for kind in ("parent", "merged", "split", "non-merged")}
        self.send_json(200, {"added": added, "found": len(imported), "sets": counts, "playlists": ["Vertical", "Requires CHD"] if make_playlists else []})

    def add_import_exclusion(self, payload):
        source = str(payload.get("source", "")).strip()
        external_id = str(payload.get("external_id", "")).strip()
        if not source or not external_id:
            raise BadRequest("source and external_id are required.")
        heroic_source = str(payload.get("heroic_source", "")).strip()
        def mutate(state):
            return add_exclusion(state, source, external_id, heroic_source=heroic_source)
        _, entry = transact_state(mutate)
        self.send_json(200, {"exclusion": entry})

    def remove_import_exclusion(self, payload):
        source = str(payload.get("source", "")).strip()
        external_id = str(payload.get("external_id", "")).strip()
        if not source or not external_id:
            raise BadRequest("source and external_id are required.")
        def mutate(state):
            return remove_exclusion(state, source, external_id)
        _, count = transact_state(mutate)
        self.send_json(200, {"removed": count > 0, "count": count})

    def _merge_imported_games(self, imported, identity_fn):
        return merge_imported_games(imported, identity_fn)

    def import_scummvm_games(self):
        added, found = self._merge_imported_games(
            import_scummvm(),
            lambda game: ("scummvm", str(game.get("scummvm_id", ""))),
        )
        _send_import_result(self, added, found)

    def import_rpcs3_games(self):
        added, found = self._merge_imported_games(
            import_rpcs3_hdd(),
            lambda game: ("rpcs3", str(game.get("path", ""))),
        )
        _send_import_result(self, added, found)

    def import_vita3k_games(self):
        added, found = self._merge_imported_games(
            import_vita3k(),
            lambda game: ("vita3k", str(game.get("path", ""))),
        )
        _send_import_result(self, added, found)

    def import_legacy_games(self, payload, importer, label):
        folder = _required_folder_path(payload)
        errors = []
        imported = importer(folder, errors)
        added, found = self._merge_imported_games(
            imported,
            lambda game: (label.casefold(), str(game.get("path", ""))),
        )
        self.send_json(200, {"added": added, "found": found, "errors": errors[:100]})
        return

    def import_storefront_catalog(self, payload):
        source = str(payload.get("source", "")).strip()
        if not source:
            raise BadRequest("source is required.")
        source_key = source.casefold()
        if source_key not in SUPPORTED_STOREFRONT_IMPORT_SOURCES:
            raise BadRequest("Storefront source must be steam, heroic, lutris, or gameyfin.")
        settings = load_state().get("settings", {})
        catalog = storefront_catalog(source, settings=settings)
        imported = catalog_entries_to_games(
            catalog,
            uninstalled_only=bool(payload.get("uninstalled_only")),
            installed_only=bool(payload.get("installed_only")),
        )
        if source_key == "steam":
            def identity(game):
                return ("steam", str(game.get("steam_app_id", "")))
        elif source_key == "heroic":
            def identity(game):
                return ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", "")))
        elif source_key == "lutris":
            def identity(game):
                return ("lutris", str(game.get("lutris_id", "")))
        else:
            def identity(game):
                return ("gameyfin", str(game.get("gameyfin_id", "")))
        if payload.get("only_new"):
            existing = {
                identity(game)
                for game in load_state().get("games", [])
                if isinstance(game, dict)
            }
            imported = [game for game in imported if identity(game) not in existing]
        added, found = merge_imported_games(imported, identity)
        _send_import_result(self, added, found, imported=len(imported))
