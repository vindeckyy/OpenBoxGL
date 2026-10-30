"""DataHandlers capability handlers. Saves, save tools, highscores, Gameyfin, and platform documents."""

import copy
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadRequest, DocumentNotFound, GameNotFound, PlatformDocumentNotFound
from handlers._shared import clean_extras
from openbox import DATA, load_state, local_only_mutation
from routes.registry import route
from pkg.parity.parity_gameyfin import GameyfinError, catalog_gameyfin, gameyfin_settings, install_gameyfin_game, test_gameyfin_connection, uninstall_gameyfin_game, validate_gameyfin_id
from pkg.parity.parity_integrations import export_highscores, import_highscores, read_local_highscores
from pkg.parity.parity_save_tools import run_hoard, run_ludusavi, save_tool_status
from pkg.parity.parity_saves import enforce_backup_limit, extra_save_candidates, scan_all_saves
from pkg.parity.parity_highscores import table as highscore_table
from pkg.parity.parity_netplay import lobby as netplay_lobby
from pkg.platform_compat import open_path
from saves import backup_saves, discover_save_paths, inspect_save_backup, list_backup_metadata, preview_restore, restore_saves
from webapp_state import INSTALLS, JOB_MANAGER, PROCESS_LOCK, approved_media_path, game_from_payload, game_from_query, load_state_view, resolve_library_game, safe_document_file, sanitize_document_records, transact_state


class DataHandlers:
    def _save_management_enabled(self):
        settings = load_state_view().get("settings", {})
        return settings.get("save_management_enabled", True) is not False

    def _save_management_off(self, *, read=False):
        if self._save_management_enabled():
            return False
        if read:
            self.send_json(200, {"enabled": False})
        else:
            raise BadRequest("Save management is disabled.")
        return True

    @route("GET", "/api/saves")
    def _api_get_api_saves(self, parsed):
        if self._save_management_off(read=True):
            return
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            backups = list_backup_metadata(game, DATA.parent / "save-backups")[:200]
            self.send_json(200, {"backups": backups})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/saves/discover")
    def _api_get_api_saves_discover(self, parsed):
        if self._save_management_off(read=True):
            return
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
            safe_name = re.sub(r'[\r\n"]', "_", path.name)
            self.send_file(
                200,
                path,
                content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                extra_headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
                frameable=True,
            )
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise DocumentNotFound("Document not found") from None
        return

    @route("GET", "/api/saves/scan")
    def _api_get_api_saves_scan(self, parsed):
        if self._save_management_off(read=True):
            return
        found = scan_all_saves(load_state_view()["games"])
        self.send_json(200, {"games": {str(key): value for key, value in found.items()}, "count": len(found)})
        return

    @route("GET", "/api/v2/netplay/lobby")
    def _api_get_api_v2_netplay_lobby(self, parsed):
        self.send_json(200, netplay_lobby())
        return

    @route("GET", "/api/v2/highscores/table")
    def _api_get_api_v2_highscores_table(self, parsed):
        try:
            game = game_from_query(load_state_view(), parse_qs(parsed.query))
            self.send_json(200, highscore_table(game))
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
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
            safe_name = re.sub(r'[\r\n"]', "_", path.name)
            self.send_file(
                200,
                path,
                content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                extra_headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
                frameable=True,
            )
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise PlatformDocumentNotFound("Platform document not found") from None
        return

    @route("GET", "/api/bigbox/startup-video")
    def _api_get_api_bigbox_startup_video(self, parsed):
        path_text = str(
            load_state_view().get("settings", {}).get("bigbox_startup_video", "") or ""
        ).strip()
        path = Path(path_text).expanduser()
        if path.suffix.lower() not in {".mp4", ".webm", ".ogv", ".ogg", ".mkv"} or not path.is_file():
            raise PlatformDocumentNotFound("Startup video not found") from None
        self.send_file(
            200,
            path,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        return

    @route("GET", "/api/platform/videos")
    def _api_get_api_platform_videos(self, parsed):
        videos = load_state_view().get("settings", {}).get("platform_videos", {})
        self.send_json(200, {"videos": videos if isinstance(videos, dict) else {}})
        return

    @route("GET", "/api/platform/video")
    def _api_get_api_platform_video(self, parsed):
        query = parse_qs(parsed.query)
        platform = str(query.get("platform", [""])[0]).strip()
        path_text = load_state_view().get("settings", {}).get("platform_videos", {}).get(platform, "")
        if not path_text:
            raise PlatformDocumentNotFound("Platform video not found") from None
        path = Path(path_text).expanduser()
        if path.suffix.lower() not in {".mp4", ".webm", ".ogv", ".ogg", ".mkv"} or not path.is_file():
            raise PlatformDocumentNotFound("Platform video not found") from None
        self.send_file(200, path, content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        return

    @route("POST", "/api/platform/videos")
    def _api_post_api_platform_videos(self, payload):
        platform = str(payload.get("platform", "")).strip()
        if not platform or len(platform) > 80:
            raise BadRequest("Platform is required.")
        if payload.get("clear"):
            def clear(state):
                state.setdefault("settings", {}).setdefault("platform_videos", {}).pop(platform, None)
            transact_state(clear)
            self.send_json(200, {"platform": platform, "path": ""})
            return
        path_text = str(payload.get("path", "")).strip()
        path = Path(path_text).expanduser()
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in {".mp4", ".webm", ".ogv", ".ogg", ".mkv"}:
            raise BadRequest("Platform video must be an existing video file.")
        def save(state):
            state.setdefault("settings", {}).setdefault("platform_videos", {})[platform] = str(path)
        transact_state(save)
        self.send_json(200, {"platform": platform, "path": str(path)})
        return

    @route("POST", "/api/v2/platform/rename")
    def _api_post_api_v2_platform_rename(self, payload):
        from pkg.parity.parity_platform_admin import rename_platform
        old = str(payload.get("old", "")).strip()
        new = str(payload.get("new", "")).strip()
        migrate_media = bool(payload.get("migrate_media"))
        def mutate(state):
            return rename_platform(state, old, new, migrate_media=migrate_media)
        _, result = transact_state(mutate)
        self.send_json(200, result)

    @route("POST", "/api/v2/platform/delete")
    def _api_post_api_v2_platform_delete(self, payload):
        from pkg.parity.parity_platform_admin import delete_platform
        name = str(payload.get("name", "")).strip()
        if str(payload.get("confirm", "")) != name:
            raise BadRequest("Type the platform name to confirm deletion.")
        delete_media = bool(payload.get("delete_media"))
        def mutate(state):
            return delete_platform(state, name, delete_media=delete_media)
        _, result = transact_state(mutate)
        self.send_json(200, result)

    @route("POST", "/api/v2/bulk/find-replace")
    def _api_post_api_v2_bulk_find_replace(self, payload):
        from pkg.parity.parity_bulk_edit import find_replace
        dry_run = payload.get("dry_run", True) is not False
        if dry_run:
            result = find_replace(load_state_view().get("games", []), payload.get("ids", []), payload.get("field"), payload.get("find"), payload.get("replace", ""), dry_run=True)
        else:
            def mutate(state):
                return find_replace(state.get("games", []), payload.get("ids", []), payload.get("field"), payload.get("find"), payload.get("replace", ""), dry_run=False)
            _, result = transact_state(mutate)
        self.send_json(200, result)

    @route("GET", "/api/v2/integration-tokens")
    def _api_get_api_v2_integration_tokens(self, parsed):
        from pkg.parity.parity_integration_tokens import masked_tokens, PROVIDERS
        settings = load_state_view().get("settings", {})
        tokens = settings.get("integration_tokens", {})
        self.send_json(200, {"providers": list(PROVIDERS), "tokens": masked_tokens(tokens)})

    @route("POST", "/api/v2/integration-tokens")
    def _api_post_api_v2_integration_tokens(self, payload):
        from pkg.parity.parity_integration_tokens import validate_provider, validate_token
        provider = validate_provider(payload.get("provider", ""))
        token = validate_token(payload.get("token", ""))
        def mutate(state):
            state.setdefault("settings", {}).setdefault("integration_tokens", {})[provider] = token
        transact_state(local_only_mutation(mutate))
        self.send_json(200, {"provider": provider, "saved": True})

    @route("POST", "/api/v2/integration-tokens/remove")
    def _api_post_api_v2_integration_tokens_remove(self, payload):
        from pkg.parity.parity_integration_tokens import validate_provider
        provider = validate_provider(payload.get("provider", ""))
        def mutate(state):
            state.setdefault("settings", {}).setdefault("integration_tokens", {}).pop(provider, None)
        transact_state(local_only_mutation(mutate))
        self.send_json(200, {"provider": provider, "removed": True})

    def _steam_action(self, app_id, action):
        app_id = str(app_id or "").strip()
        if not app_id.isdigit() or not 0 < int(app_id) < 100000000:
            raise BadRequest("A numeric Steam app id is required.")
        action = str(action or "install").strip()
        if action not in {"install", "repair"}:
            raise BadRequest("Steam action must be install or repair.")
        verb = "validate" if action == "repair" else "install"
        try:
            open_path(f"steam://{verb}/{app_id}")
        except OSError as error:
            raise BadRequest(f"Steam did not open: {error}") from None
        return app_id, action

    @route("POST", "/api/v2/steam/action")
    def _api_post_api_v2_steam_action(self, payload):
        app_id, action = self._steam_action(payload.get("app_id"), payload.get("action"))
        self.send_json(200, {"app_id": app_id, "action": action})

    @route("POST", "/api/game/store-action")
    def _api_post_api_game_store_action(self, payload):
        try:
            game = game_from_payload(load_state_view(), payload)
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        app_id = str(game.get("steam_app_id") or "").strip()
        if not app_id:
            raise BadRequest("This game has no Steam app id.")
        app_id, action = self._steam_action(app_id, payload.get("action"))
        self.send_json(200, {"game_id": game.get("game_id"), "app_id": app_id, "action": action})

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

    @route("POST", "/api/saves/restore/preview")
    def _api_post_api_saves_restore_preview(self, payload):
        if self._save_management_off():
            return
        game = game_from_payload(load_state(), payload)
        backup_name = str(payload.get("backup") or "").strip()
        if not backup_name:
            raise BadRequest("A backup name is required.")
        selected = payload.get("selected")
        if selected is not None and not isinstance(selected, list):
            raise BadRequest("selected must be a list of archive paths.")
        try:
            preview = preview_restore(game, DATA.parent / "save-backups", backup_name, selected)
        except (OSError, ValueError) as error:
            raise BadRequest(str(error)) from None
        self.send_json(200, preview)
        return

    @route("POST", "/api/saves/restore")
    def _api_post_api_saves_restore(self, payload):
        self.restore_game_saves(payload)

    @route("GET", "/api/saves/manifest")
    def _api_get_api_saves_manifest(self, parsed):
        if self._save_management_off(read=True):
            return
        query = parse_qs(parsed.query)
        game = game_from_query(load_state(), query)
        backup_name = str(query.get("backup", [""])[0]).strip()
        if not backup_name:
            raise BadRequest("A backup name is required.")
        try:
            result = inspect_save_backup(game, DATA.parent / "save-backups", backup_name)
        except (OSError, ValueError) as error:
            raise BadRequest(str(error)) from None
        self.send_json(200, result)
        return

    @route("POST", "/api/saves/add")
    def _api_post_api_saves_add(self, payload):
        self.add_game_save_path(payload)

    def backup_game_saves(self, payload):
        if self._save_management_off():
            return
        game = game_from_payload(load_state(), payload)
        game_id = str(game.get("game_id") or "")

        def worker(_cancel_event):
            archive = backup_saves(game, DATA.parent / "save-backups")
            removed = enforce_backup_limit(
                game,
                DATA.parent / "save-backups",
                load_state().get("settings", {}).get("save_backup_limit", 10),
            )
            return {"backup": archive.name, "trimmed": removed, "game_id": game_id}

        job = JOB_MANAGER.submit(f"saves-backup:{game_id or 'game'}", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def restore_game_saves(self, payload):
        if self._save_management_off():
            return
        game = game_from_payload(load_state(), payload)
        game_id = str(game.get("game_id") or "")
        backup_name = str(payload["backup"])
        selected = payload.get("selected")
        dry_run = bool(payload.get("dry_run"))
        if selected is not None and not isinstance(selected, list):
            raise BadRequest("selected must be a list of archive paths.")
        if not dry_run:
            try:
                preview = preview_restore(game, DATA.parent / "save-backups", backup_name, selected)
            except (OSError, ValueError) as error:
                raise BadRequest(str(error)) from None
            if preview.get("conflict") and not payload.get("confirm_conflict"):
                self.send_json(409, {
                    "error": "Restoring this backup would overwrite local save files.",
                    "preview": preview,
                })
                return

        def worker(_cancel_event):
            archive = restore_saves(game, DATA.parent / "save-backups", backup_name, selected=selected, dry_run=dry_run)
            if isinstance(archive, dict):
                return archive
            return {"restored": archive.name, "game_id": game_id}

        job = JOB_MANAGER.submit(f"saves-restore:{game_id or 'game'}", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def add_game_save_path(self, payload):
        if self._save_management_off():
            return
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
        documents = clean_extras(payload.get("documents", []), command=False)
        for document in documents:
            document["path"] = str(approved_media_path(document["path"], must_exist=False))
        if payload.get("reorder"):
            current = load_state().get("settings", {}).get("platform_documents", {}).get(platform, [])
            current_paths = [str(item.get("path") or "") for item in current if isinstance(item, dict)]
            new_paths = [str(item.get("path") or "") for item in documents]
            if sorted(current_paths) != sorted(new_paths):
                raise ValueError("Reordering cannot add or remove platform documents.")
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

        job = JOB_MANAGER.submit(job_key, worker)
        self.send_json(202, {"state": "installing", "gameyfin_id": game_id, "job_id": job["job_id"]})

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

    @route("POST", "/api/saves/scan/apply")
    def _api_post_api_saves_scan_apply(self, payload):
        self.apply_save_scan(payload)

    def apply_save_scan(self, payload):
        if self._save_management_off():
            return
        def worker(_cancel_event):
            state = load_state()
            found = scan_all_saves(state["games"])
            found_by_id = {
                str(state["games"][index].get("game_id")): paths
                for index, paths in found.items()
                if 0 <= index < len(state["games"])
            }
            sources_by_id = {
                str(state["games"][index].get("game_id")): state["games"][index].get("save_sources") or {}
                for index in found
                if 0 <= index < len(state["games"])
            }

            def mutate(state):
                updated = 0
                for stable_id, paths in found_by_id.items():
                    try:
                        game = game_from_payload(state, {"game_id": stable_id})
                    except IndexError:
                        continue
                    save_paths = game.setdefault("save_paths", [])
                    for path in paths:
                        if path not in save_paths:
                            save_paths.append(path)
                            updated += 1
                    # scan_all_saves discovered per-path emulator/core/slot
                    # provenance on a copy of state; persist it alongside the
                    # paths or the next launch cannot restore with the right
                    # core.
                    sources = game.setdefault("save_sources", {})
                    for path, metadata in (sources_by_id.get(stable_id) or {}).items():
                        sources[path] = metadata
                return updated

            _, updated = transact_state(mutate)
            return {"updated": updated, "games": len(found)}

        job = JOB_MANAGER.submit("saves-scan", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})
