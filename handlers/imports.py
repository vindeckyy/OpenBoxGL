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
from importers import import_heroic, import_lutris, import_steam
from openbox import load_state
from parity_import import import_rpcs3_hdd, import_scummvm, import_vita3k
from parity_import_policy import add_exclusion, list_exclusions, remove_exclusion
from parity_premium import import_loose_arcade, import_xbox360_folder
from parity_storefront import catalog_entries_to_games, storefront_catalog
from webapp_state import (
    broadcast_event,
    clear_file_probe_cache,
    import_folder_path,
    load_state_view,
    merge_imported_games,
    transact_state,
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
        from pkg.parity.parity_launchbox_import import create_launchbox_preview

        payload = payload or {}
        xml_path = str(payload.get("xml_path", "")).strip()
        if not xml_path:
            raise BadRequest("xml_path is required.")
        if not Path(xml_path).is_file():
            raise BadRequest(f"LaunchBox XML not found: {xml_path}")
        state = load_state_view()
        report = create_launchbox_preview(xml_path, state.get("games", []))
        self.send_json(200, report)

    @route("POST", "/api/v2/import/launchbox/resolve")
    def _api_post_api_v2_import_launchbox_resolve(self, payload):
        from pkg.parity.parity_launchbox_import import resolve_launchbox_preview

        payload = payload or {}
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        mappings = payload.get("mappings") or {}
        if not isinstance(mappings, dict):
            raise BadRequest("mappings must be an object.")
        path_remap = payload.get("path_remap")
        if path_remap is not None and not isinstance(path_remap, dict):
            raise BadRequest("path_remap must be an object.")
        state = load_state_view()
        result = resolve_launchbox_preview(preview_id, mappings, path_remap, state.get("games", []))
        self.send_json(200, result)

    @route("POST", "/api/v2/import/launchbox/apply")
    def _api_post_api_v2_import_launchbox_apply(self, payload):
        from pkg.parity.parity_launchbox_import import apply_import

        xml_path = str(payload.get("xml_path", "")).strip()
        if not xml_path:
            raise BadRequest("xml_path is required.")
        if not Path(xml_path).is_file():
            raise BadRequest(f"LaunchBox XML not found: {xml_path}")
        state = load_state_view()
        result = apply_import(xml_path, state["games"], merge_imported_games)
        clear_file_probe_cache()
        broadcast_event("library.imported", {"source": "launchbox", "added": result.get("added", 0)})
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
        for app_id in chosen.values():
            if not app_id:
                continue
            try:
                install_emulator(str(app_id))
                installs.append(str(app_id))
            except (OSError, ValueError, RuntimeError) as e:
                LOGGER.warning("install_emulator %s: %s", app_id, e)
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
        imported = import_steam()
        added, found = merge_imported_games(imported, lambda game: ("steam", str(game.get("steam_app_id", ""))))
        _send_import_result(self, added, found)

    def import_heroic_games(self):
        imported = import_heroic()
        added, found = merge_imported_games(
            imported,
            lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", ""))),
        )
        _send_import_result(self, added, found)

    def import_lutris_games(self):
        imported = import_lutris()
        added, found = merge_imported_games(imported, lambda game: ("lutris", str(game.get("lutris_id", ""))))
        _send_import_result(self, added, found)
    def import_arcade_games(self, payload):
        folder = _required_folder_path(payload)
        imported = import_arcade(
            folder,
            str(payload.get("dat", "")),
            str(payload.get("command", "")),
            str(payload.get("source", "MAME")),
        )
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
            return len(new_games)
        _, added = transact_state(mutate)
        clear_file_probe_cache()
        counts = {kind: sum(game["set_type"] == kind for game in imported) for kind in ("parent", "merged", "split", "non-merged")}
        self.send_json(200, {"added": added, "found": len(imported), "sets": counts})

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
        added, found = merge_imported_games(imported, identity)
        _send_import_result(self, added, found, imported=len(imported))
