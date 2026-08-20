"""DataHandlers capability handlers. Saves, save tools, highscores, Gameyfin, and platform documents."""

import copy
import mimetypes
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadRequest, DocumentNotFound, GameNotFound, PlatformDocumentNotFound
from openbox import DATA, load_state
from routes.registry import route
from parity_gameyfin import GameyfinError, catalog_gameyfin, gameyfin_settings, install_gameyfin_game, test_gameyfin_connection, uninstall_gameyfin_game, validate_gameyfin_id
from parity_integrations import export_highscores, import_highscores, read_local_highscores
from parity_save_tools import run_hoard, run_ludusavi, save_tool_status
from parity_saves import enforce_backup_limit, extra_save_candidates, scan_all_saves
from saves import backup_saves, discover_save_paths, list_backups, restore_saves
from webapp_state import INSTALLS, JOB_MANAGER, PROCESS_LOCK, approved_media_path, game_from_payload, game_from_query, load_state_view, resolve_library_game, safe_document_file, sanitize_document_records, transact_state


class DataHandlers:
    @route("GET", "/api/saves")
    def _api_get_api_saves(self, parsed):
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            backups = [{"name": path.name, "size": path.stat().st_size} for path in list_backups(game, DATA.parent / "save-backups")]
            self.send_json(200, {"backups": backups})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/saves/discover")
    def _api_get_api_saves_discover(self, parsed):
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            configured = set(game.get("save_paths", []))
            candidates = [
                item for item in discover_save_paths(game) + extra_save_candidates(game)
                if item["path"] not in configured
            ]
            self.send_json(200, {"candidates": candidates})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/document")
    def _api_get_api_document(self, parsed):
        query = parse_qs(parsed.query)
        try:
            game = game_from_query(load_state_view(), query)
            document = game.get("documents", [])[int(query["index"][0])]
            path = safe_document_file(document["path"])
            self.send_response(200)
            self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            safe_name = re.sub(r'[\r\n"]', "_", path.name)
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise DocumentNotFound("Document not found") from None
        return

    @route("GET", "/api/saves/scan")
    def _api_get_api_saves_scan(self, parsed):
        found = scan_all_saves(load_state_view()["games"])
        self.send_json(200, {"games": {str(key): value for key, value in found.items()}, "count": len(found)})
        return

    @route("GET", "/api/highscores")
    def _api_get_api_highscores(self, parsed):
        try:
            game = game_from_query(load_state(), parse_qs(parsed.query))
            self.send_json(200, {"scores": read_local_highscores(game)})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/platform/documents")
    def _api_get_api_platform_documents(self, parsed):
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        docs = load_state_view().get("settings", {}).get("platform_documents", {})
        if platform:
            result = sanitize_document_records(docs.get(platform, [])) if isinstance(docs, dict) else []
        else:
            result = {
                str(name): sanitize_document_records(items)
                for name, items in docs.items()
            } if isinstance(docs, dict) else {}
        self.send_json(200, {"documents": result})
        return

    @route("GET", "/api/platform/document")
    def _api_get_api_platform_document(self, parsed):
        query = parse_qs(parsed.query)
        try:
            platform = query["platform"][0]
            index = int(query["index"][0])
            document = load_state_view().get("settings", {}).get("platform_documents", {}).get(platform, [])[index]
            path = safe_document_file(document["path"])
            self.send_response(200)
            self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            safe_name = re.sub(r'[\r\n"]', "_", path.name)
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise PlatformDocumentNotFound("Platform document not found") from None
        return

    @route("GET", "/api/gameyfin/install/status")
    def _api_get_api_gameyfin_install_status(self, parsed):
        query = parse_qs(parsed.query)
        raw_gameyfin_id = str(query.get("gameyfin_id", [""])[0]).strip()
        if not raw_gameyfin_id:
            raise BadRequest("gameyfin_id is required.")
        gameyfin_id = validate_gameyfin_id(raw_gameyfin_id)
        with PROCESS_LOCK:
            job = dict(INSTALLS.get(f"gameyfin:{gameyfin_id}", {"state": "idle"}))
        self.send_json(200, job)
        return

    @route("GET", "/api/gameyfin/providers")
    def _api_get_api_gameyfin_providers(self, parsed):
        try:
            _catalog, providers = catalog_gameyfin(load_state_view().get("settings", {}))
            self.send_json(200, {"providers": providers})
        except (ValueError, OSError, TypeError, AttributeError) as error:
            raise BadRequest(str(error)) from None
        return

    @route("GET", "/api/save-tools/status")
    def _api_get_api_save_tools_status(self, parsed):
        self.send_json(200, save_tool_status())
        return

    @route("POST", "/api/platform/documents")
    def _api_post_api_platform_documents(self, payload):
        self.save_platform_documents(payload)

    @route("POST", "/api/gameyfin/test")
    def _api_post_api_gameyfin_test(self, payload):
        self.test_gameyfin(payload)

    @route("POST", "/api/gameyfin/install")
    def _api_post_api_gameyfin_install(self, payload):
        self.install_gameyfin(payload)

    @route("POST", "/api/gameyfin/uninstall")
    def _api_post_api_gameyfin_uninstall(self, payload):
        self.uninstall_gameyfin(payload)

    @route("POST", "/api/save-tools/ludusavi")
    def _api_post_api_save_tools_ludusavi(self, payload):
        self.run_ludusavi_tool(payload)

    @route("POST", "/api/save-tools/hoard")
    def _api_post_api_save_tools_hoard(self, payload):
        self.run_hoard_tool(payload)

    @route("POST", "/api/highscores/export")
    def _api_post_api_highscores_export(self, payload):
        self.export_game_highscores(payload)

    @route("POST", "/api/highscores/import")
    def _api_post_api_highscores_import(self, payload):
        self.import_game_highscores(payload)

    @route("POST", "/api/saves/backup")
    def _api_post_api_saves_backup(self, payload):
        self.backup_game_saves(payload)

    @route("POST", "/api/saves/restore")
    def _api_post_api_saves_restore(self, payload):
        self.restore_game_saves(payload)

    @route("POST", "/api/saves/add")
    def _api_post_api_saves_add(self, payload):
        self.add_game_save_path(payload)

    def backup_game_saves(self, payload):
        game = game_from_payload(load_state(), payload)
        archive = backup_saves(game, DATA.parent / "save-backups")
        removed = enforce_backup_limit(game, DATA.parent / "save-backups", load_state().get("settings", {}).get("save_backup_limit", 10))
        self.send_json(200, {"backup": archive.name, "trimmed": removed})

    def restore_game_saves(self, payload):
        game = game_from_payload(load_state(), payload)
        archive = restore_saves(game, DATA.parent / "save-backups", str(payload["backup"]))
        self.send_json(200, {"restored": archive.name})

    def add_game_save_path(self, payload):
        path = Path(str(payload.get("path", ""))).expanduser()
        if not path.exists():
            raise FileNotFoundError("Save path does not exist.")
        def mutate(state):
            paths = game_from_payload(state, payload).setdefault("save_paths", [])
            if str(path) not in paths:
                paths.append(str(path))
        transact_state(mutate)
        self.send_json(200, {"path":str(path)})

    def save_platform_documents(self, payload):
        platform = str(payload.get("platform", "")).strip()
        if not platform:
            raise ValueError("Platform is required.")
        documents = self.clean_extras(payload.get("documents", []), command=False)
        for document in documents:
            document["path"] = str(approved_media_path(document["path"], must_exist=False))
        def mutate(state):
            settings = state.setdefault("settings", {})
            settings.setdefault("platform_documents", {})[platform] = documents
        transact_state(mutate)
        self.send_json(200, {"saved": platform, "count": len(documents)})

    def test_gameyfin(self, payload):
        settings = dict(load_state().get("settings", {}))
        for key, value in (payload or {}).items():
            if key == "gameyfin_password" and not str(value or "").strip():
                continue
            settings[key] = value
        result = test_gameyfin_connection(settings)
        self.send_json(200, result)

    def install_gameyfin(self, payload):
        raw_game_id = str(payload.get("gameyfin_id") or payload.get("id") or "").strip()
        if not raw_game_id:
            raise ValueError("gameyfin_id is required.")
        game_id = validate_gameyfin_id(raw_game_id)
        library_id = payload.get("library_id")
        stable_library_id = ""
        if library_id is not None:
            try:
                library_state = load_state()
                stable_library_id = str(game_from_payload(library_state, {"id": library_id}).get("game_id") or "")
            except (ValueError, IndexError):
                stable_library_id = str(library_id)
        job_key = f"gameyfin:{game_id}"
        with PROCESS_LOCK:
            job = INSTALLS.get(job_key, {})
            if job.get("state") == "installing":
                self.send_json(200, {"state": "installing", "gameyfin_id": game_id})
                return
            INSTALLS[job_key] = {"state": "installing", "gameyfin_id": game_id}

        def worker():
            result = {"state": "error", "gameyfin_id": game_id, "error": "Install failed"}
            try:
                settings = dict(load_state().get("settings", {}))
                installed = install_gameyfin_game(settings, game_id)
                def mutate(state):
                    target = None
                    for game in state["games"]:
                        if str(game.get("gameyfin_id") or "") == game_id:
                            target = game
                            break
                    if target is None and stable_library_id:
                        target = resolve_library_game(state, {"stable_game_id": stable_library_id})
                    if target is None and library_id is not None:
                        try:
                            index = int(library_id)
                        except (TypeError, ValueError):
                            index = -1
                        if 0 <= index < len(state["games"]):
                            candidate = state["games"][index]
                            existing_id = str(candidate.get("gameyfin_id") or "")
                            if not existing_id or existing_id == game_id:
                                target = candidate
                    if target is not None:
                        target.update(installed)
                    else:
                        state["games"].append(installed)
                transact_state(mutate)
                result = {"state": "done", "gameyfin_id": game_id, "game": installed}
            except (GameyfinError, OSError, ValueError, IndexError, KeyError) as error:
                result = {"state": "error", "gameyfin_id": game_id, "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[job_key] = result

        JOB_MANAGER.submit(job_key, worker)
        self.send_json(202, {"state": "installing", "gameyfin_id": game_id})

    def uninstall_gameyfin(self, payload):
        state = load_state()
        original = game_from_payload(state, payload)
        target = copy.deepcopy(original)
        if not target.get("gameyfin_id"):
            raise ValueError("This game is not a Gameyfin entry.")
        settings = gameyfin_settings(state.get("settings", {}))
        install_root = settings["install_dir"] or str(Path.home() / "Games" / "Gameyfin")
        result = uninstall_gameyfin_game(target, install_root)
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        self.send_json(200, result)

    def run_ludusavi_tool(self, payload):
        settings = load_state().get("settings", {})
        game_name = str(payload.get("name", ""))
        if "id" in payload and not game_name:
            game_name = game_from_payload(load_state(), payload).get("name", "")
        result = run_ludusavi(
            str(payload.get("action", "backup")),
            game_name=game_name,
            path=str(payload.get("path") or settings.get("ludusavi_backup_path", "")),
        )
        self.send_json(200, result)

    def run_hoard_tool(self, payload):
        game_name = str(payload.get("name", ""))
        if "id" in payload and not game_name:
            game_name = game_from_payload(load_state(), payload).get("name", "")
        result = run_hoard(str(payload.get("action", "backup")), game_name=game_name)
        self.send_json(200, result)

    def export_game_highscores(self, payload):
        state = load_state()
        game = game_from_payload(state, payload)
        export_dir = DATA.parent / "highscores" / re.sub(r"[^a-z0-9]+", "-", str(game.get("name", "game")).casefold()).strip("-")
        result = export_highscores(game, export_dir)
        self.send_json(200, result)

    def import_game_highscores(self, payload):
        import_dir = str(payload.get("path", "")).strip()
        state = load_state()
        game = game_from_payload(state, payload)
        restored = import_highscores(game, import_dir)
        self.send_json(200, {"restored": restored})
