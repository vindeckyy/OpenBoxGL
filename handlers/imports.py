"""ImportsHandlers capability handlers. Import sources, storefront catalogs, and import exclusions."""

import subprocess
from datetime import datetime
from urllib.parse import parse_qs

from api_errors import BadRequest
from arcade import import_arcade
from emulators import install_emulator
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


SUPPORTED_STOREFRONT_IMPORT_SOURCES = {"steam", "heroic", "lutris", "gameyfin"}

def _required_folder_path(payload):
    value = payload.get("folder", "")
    if value is None:
        raise BadRequest("Folder path is required.")
    folder = str(value)
    if not folder.strip():
        raise BadRequest("Folder path is required.")
    return folder


class ImportsHandlers:
    def _api_get_api_storefront_catalog(self, parsed):
        source = parse_qs(parsed.query).get("source", [""])[0]
        try:
            self.send_json(200, {"catalog": storefront_catalog(source, settings=load_state_view().get("settings", {}))})
        except (ValueError, OSError, FileNotFoundError, subprocess.SubprocessError) as error:
            self.send_json(400, {"error": str(error)})
        return

    def _api_get_api_import_exclusions(self, parsed):
        self.send_json(200, {"exclusions": list_exclusions(load_state_view())})
        return

    def _api_post_api_import(self, payload):
        self.import_folder(payload)

    def _api_post_api_import_wizard(self, payload):
        self.import_wizard(payload)

    def _api_post_api_import_xbox360(self, payload):
        self.import_xbox360(payload)

    def _api_post_api_import_loose_arcade(self, payload):
        self.import_loose_arcade_route(payload)

    def _api_post_api_import_watch(self, payload):
        self.scan_watch_folders()

    def _api_post_api_import_steam(self, payload):
        self.import_steam_games()

    def _api_post_api_import_heroic(self, payload):
        self.import_heroic_games()

    def _api_post_api_import_lutris(self, payload):
        self.import_lutris_games()

    def _api_post_api_import_arcade(self, payload):
        self.import_arcade_games(payload)

    def _api_post_api_import_scummvm(self, payload):
        self.import_scummvm_games()

    def _api_post_api_import_rpcs3(self, payload):
        self.import_rpcs3_games()

    def _api_post_api_import_vita3k(self, payload):
        self.import_vita3k_games()

    def _api_post_api_storefront_import(self, payload):
        self.import_storefront_catalog(payload)

    def _api_post_api_import_exclusions(self, payload):
        self.add_import_exclusion(payload)

    def _api_post_api_import_exclusions_delete(self, payload):
        self.remove_import_exclusion(payload)

    def import_folder(self, payload):
        folder = _required_folder_path(payload)
        broadcast_event("job.progress", {"job": "import", "folder": folder, "state": "running"})
        added, found, recommendations = import_folder_path(
            folder,
            chosen_emulators=payload.get("chosen_emulators"),
        )
        clear_file_probe_cache()
        broadcast_event("job.progress", {"job": "import", "folder": folder, "added": added, "found": found, "state": "done"})
        self.send_json(200, {"added": added, "found": found, "recommendations": recommendations})

    def import_wizard(self, payload):
        folder = _required_folder_path(payload)
        chosen = payload.get("chosen_emulators", {})
        if not isinstance(chosen, dict):
            raise BadRequest("chosen_emulators must be an object.")
        added, found, recommendations = import_folder_path(folder, chosen_emulators=chosen)
        clear_file_probe_cache()
        installs = []
        for app_id in chosen.values():
            if not app_id:
                continue
            try:
                install_emulator(str(app_id))
                installs.append(str(app_id))
            except (OSError, ValueError, RuntimeError):
                pass
        self.send_json(200, {"added": added, "found": found, "recommendations": recommendations, "installed": installs})

    def import_xbox360(self, payload):
        folder = _required_folder_path(payload)
        imported = import_xbox360_folder(folder, str(payload.get("command", "")))
        added, found = merge_imported_games(imported, lambda game: ("path", game.get("path", "")))
        self.send_json(200, {"added": added, "found": found})

    def import_loose_arcade_route(self, payload):
        folder = _required_folder_path(payload)
        imported = import_loose_arcade(folder, str(payload.get("command", "")))
        added, found = merge_imported_games(imported, lambda game: ("path", game.get("path", "")))
        self.send_json(200, {"added": added, "found": found})

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
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found})

    def import_heroic_games(self):
        imported = import_heroic()
        added, found = merge_imported_games(
            imported,
            lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", ""))),
        )
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found})

    def import_lutris_games(self):
        imported = import_lutris()
        added, found = merge_imported_games(imported, lambda game: ("lutris", str(game.get("lutris_id", ""))))
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found})

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
        self.send_json(200, {"added": added, "found": found})

    def import_rpcs3_games(self):
        added, found = self._merge_imported_games(
            import_rpcs3_hdd(),
            lambda game: ("rpcs3", str(game.get("path", ""))),
        )
        self.send_json(200, {"added": added, "found": found})

    def import_vita3k_games(self):
        added, found = self._merge_imported_games(
            import_vita3k(),
            lambda game: ("vita3k", str(game.get("path", ""))),
        )
        self.send_json(200, {"added": added, "found": found})

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
        self.send_json(200, {"added": added, "found": found, "imported": len(imported)})
