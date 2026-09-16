"""DataHandlers capability handlers. Saves, save tools, highscores, Gameyfin, and platform documents."""

import copy
import csv
import io
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadRequest, DocumentNotFound, GameNotFound, PlatformDocumentNotFound
from handlers._shared import clean_extras
from openbox import DATA, load_state
from routes.registry import route
from parity_gameyfin import GameyfinError, catalog_gameyfin, gameyfin_settings, install_gameyfin_game, test_gameyfin_connection, uninstall_gameyfin_game, validate_gameyfin_id
from parity_integrations import export_highscores, import_highscores, read_local_highscores
from parity_save_tools import run_hoard, run_ludusavi, save_tool_status
from parity_saves import enforce_backup_limit, extra_save_candidates, scan_all_saves
from pkg.parity.parity_household import compute_leaderboard
from pkg.parity.parity_save_history import apply_retention, history_response, test_restore, verify_version
from saves import backup_saves, discover_save_paths, list_backups, restore_saves
from webapp_state import INSTALLS, JOB_MANAGER, PROCESS_LOCK, approved_media_path, game_from_payload, game_from_query, load_state_view, resolve_library_game, safe_document_file, sanitize_document_records, transact_state


SAVE_ROOTS_ENV = "OPENBOX_SAVE_ROOTS"

# Session/journal exports are generated on demand: rows are capped before
# rendering so one library with a 500-row history can never build an unbounded
# response, and the text is streamed from a temp file by send_file.
SESSION_EXPORT_MAX_ROWS = 5000
SESSION_EXPORT_FORMATS = ("md", "csv")
SESSION_EXPORT_SCOPES = ("game", "member")


def _save_path_roots():
    roots = []
    for value in (Path.home(), DATA.parent):
        try:
            roots.append(value.resolve())
        except (OSError, RuntimeError):
            continue
    configured = os.environ.get(SAVE_ROOTS_ENV, "")
    if len(configured) > 16 * 4096:
        raise ValueError(f"{SAVE_ROOTS_ENV} is too long.")
    for item in configured.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{SAVE_ROOTS_ENV} entries must be absolute paths.")
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Could not resolve save root: {candidate}") from error
        if resolved == Path("/"):
            raise ValueError("Filesystem root is not an approved save root.")
        roots.append(resolved)
    return roots


def approved_save_path(path):
    """Return an absolute save path under the user's home or an approved root.

    ``/etc``, other filesystem locations, and the home directory itself are
    rejected.  Extra roots opt in with ``OPENBOX_SAVE_ROOTS`` (an absolute-path
    list separated by ``os.pathsep``).
    """
    raw = Path(str(path or "")).expanduser()
    if not str(path or "").strip() or not raw.is_absolute():
        raise ValueError("Save paths must be absolute paths under the user's home or an approved save root.")
    try:
        candidate = raw.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Could not resolve save path: {raw}") from error
    if not any(candidate != root and root in candidate.parents for root in _save_path_roots()):
        raise ValueError(f"Save path is outside the user's home and approved save roots: {candidate}")
    return candidate


def _session_seconds(entry: dict) -> int:
    try:
        return max(0, int(entry.get("seconds") or entry.get("playtime_seconds") or 0))
    except (TypeError, ValueError):
        return 0


def session_export_rows(state: dict) -> list[dict]:
    """One row per recorded session, oldest first, bounded."""
    history = state.get("history") if isinstance(state, dict) else None
    if not isinstance(history, list):
        return []
    rows = []
    for entry in history[-SESSION_EXPORT_MAX_ROWS:]:
        if not isinstance(entry, dict):
            continue
        seconds = _session_seconds(entry)
        rows.append({
            "game": str(entry.get("game") or ""),
            "game_id": str(entry.get("game_id") or ""),
            "started": str(entry.get("started") or entry.get("started_at") or ""),
            "seconds": seconds,
            "minutes": round(seconds / 60, 1),
            "exit_code": entry.get("exit_code", ""),
        })
    return rows


def member_export_rows(state: dict) -> list[dict]:
    """Per-member totals from household shares (session rows carry no member)."""
    try:
        board = compute_leaderboard(state, period=None)
    except (TypeError, ValueError):
        return []
    rows = []
    for entry in board.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        rows.append({
            "member_id": str(entry.get("member_id") or ""),
            "display_name": str(entry.get("display_name") or entry.get("member_id") or ""),
            "sessions": int(entry.get("sessions") or 0),
            "playtime_seconds": int(entry.get("playtime_seconds") or 0),
            "playtime_hours": round(int(entry.get("playtime_seconds") or 0) / 3600, 1),
            "games_played": int(entry.get("games_played") or 0),
            "completions": int(entry.get("completions") or 0),
        })
    return rows[:SESSION_EXPORT_MAX_ROWS]


def _markdown_cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_session_export(scope: str, fmt: str, rows: list[dict], *, title: str = "") -> str:
    """Render bounded export rows as markdown or CSV text."""
    if scope == "game":
        columns = ("started", "game", "minutes", "seconds", "exit_code")
        labels = ("Started", "Game", "Minutes", "Seconds", "Exit code")
    else:
        columns = ("display_name", "sessions", "playtime_hours", "playtime_seconds", "games_played", "completions")
        labels = ("Member", "Sessions", "Playtime (h)", "Playtime (s)", "Games played", "Completions")
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(labels)
        for row in rows:
            writer.writerow([row.get(column, "") for column in columns])
        return buffer.getvalue()
    lines = [f"# {title or ('Play sessions' if scope == 'game' else 'Household playtime')}", ""]
    lines.append("| " + " | ".join(labels) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_markdown_cell(row.get(column, "")) for column in columns) + " |")
    if scope == "member":
        lines.append("")
        lines.append("Per-member totals come from household shares; session history itself is not member-attributed.")
    return "\n".join(lines) + "\n"


def _export_slug(value) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return slug[:48] or "export"


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
        found = scan_all_saves(load_state_view()["games"])
        self.send_json(200, {"games": {str(key): value for key, value in found.items()}, "count": len(found)})
        return

    @route("GET", "/api/v2/saves/history")
    def _api_get_api_v2_saves_history(self, parsed):
        try:
            query = parse_qs(parsed.query)
            state = load_state_view()
            game = game_from_query(state, query)
            settings = state.get("settings", {}) if isinstance(state, dict) else {}
            keep = settings.get("save_backup_limit", 10) if isinstance(settings, dict) else 10
            self.send_json(200, history_response(game, DATA.parent / "save-backups", keep=keep))
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/v2/sessions/export")
    def _api_get_api_v2_sessions_export(self, parsed):
        """Per-game or per-member sessions + playtime as streamed markdown/CSV."""
        query = parse_qs(parsed.query or "")
        fmt = (query.get("format", ["md"])[0] or "md").strip().lower()
        scope = (query.get("scope", ["game"])[0] or "game").strip().lower()
        if fmt not in SESSION_EXPORT_FORMATS:
            raise BadRequest("format must be md or csv.")
        if scope not in SESSION_EXPORT_SCOPES:
            raise BadRequest("scope must be game or member.")
        state = load_state_view()
        if scope == "game":
            game_id = (query.get("game_id", [""])[0] or "").strip()
            rows = session_export_rows(state)
            title = "Play sessions"
            if game_id:
                matching = [row for row in rows if row["game_id"] == game_id]
                if not matching:
                    game = next(
                        (item for item in state.get("games", [])
                         if isinstance(item, dict) and str(item.get("game_id") or "") == game_id),
                        None,
                    )
                    if game is not None:
                        matching = [row for row in rows if row["game"] == str(game.get("name") or "")]
                        title = str(game.get("name") or game_id)
                else:
                    title = next((str(row["game"]) for row in matching if row["game"]), game_id)
                rows = matching
            filename = f"openbox-sessions-{_export_slug(title) if game_id else 'all'}"
        else:
            rows = member_export_rows(state)
            filename = "openbox-household-playtime"
            title = "Household playtime"
        text = render_session_export(scope, fmt, rows, title=title)
        content_type = "text/csv; charset=utf-8" if fmt == "csv" else "text/markdown; charset=utf-8"
        self._stream_text_export(text, f"{filename}.{fmt}", content_type)
        return

    def _stream_text_export(self, text: str, filename: str, content_type: str):
        """Write the bounded export to a temp file and stream it from disk."""
        suffix = Path(filename).suffix or ".txt"
        try:
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=suffix, prefix="openbox-export-", delete=False
            )
        except OSError as error:
            raise BadRequest(f"Could not create the export file: {error}") from error
        path = Path(handle.name)
        try:
            with handle:
                handle.write(text)
            self.send_file(200, path, content_type, extra_headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            })
        except OSError as error:
            raise BadRequest(f"Could not write the export file: {error}") from error
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

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

    @route("POST", "/api/v2/saves/history/verify")
    def _api_post_api_v2_saves_history_verify(self, payload):
        self.verify_save_version(payload)

    @route("POST", "/api/v2/saves/history/test-restore")
    def _api_post_api_v2_saves_history_test_restore(self, payload):
        self.test_restore_save_version(payload)

    @route("POST", "/api/v2/saves/history/restore")
    def _api_post_api_v2_saves_history_restore(self, payload):
        self.restore_save_version(payload)

    @route("POST", "/api/v2/saves/history/prune")
    def _api_post_api_v2_saves_history_prune(self, payload):
        self.prune_save_history(payload)

    @route("POST", "/api/saves/add")
    def _api_post_api_saves_add(self, payload):
        self.add_game_save_path(payload)

    def backup_game_saves(self, payload):
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
        game = game_from_payload(load_state(), payload)
        game_id = str(game.get("game_id") or "")
        backup_name = str(payload["backup"])

        def worker(_cancel_event):
            archive = restore_saves(game, DATA.parent / "save-backups", backup_name)
            return {"restored": archive.name, "game_id": game_id}

        job = JOB_MANAGER.submit(f"saves-restore:{game_id or 'game'}", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def verify_save_version(self, payload):
        """Read one archive back and report its verified contents."""
        game = game_from_payload(load_state(), payload)
        name = str(payload.get("backup") or "").strip()
        if not name:
            raise BadRequest("backup is required.")
        try:
            result = verify_version(game, DATA.parent / "save-backups", name)
        except FileNotFoundError:
            raise GameNotFound("Save backup not found.") from None
        except (ValueError, OSError):
            raise BadRequest("Save backup could not be verified.", code="SAVE_BACKUP_INVALID") from None
        self.send_json(200, result)

    def test_restore_save_version(self, payload):
        """Extract one backup into a temp dir, verify it, never touch live saves."""
        game = game_from_payload(load_state_view(), payload)
        name = str(payload.get("backup") or "").strip()
        if not name:
            raise BadRequest("backup is required.")
        try:
            result = test_restore(game, DATA.parent / "save-backups", name)
        except FileNotFoundError:
            raise GameNotFound("Save backup not found.") from None
        except (ValueError, OSError):
            raise BadRequest("Save backup could not be test-restored.", code="SAVE_BACKUP_INVALID") from None
        self.send_json(200, result)

    def restore_save_version(self, payload):
        """Queue a versioned save restore through the existing checked path."""
        game = game_from_payload(load_state(), payload)
        game_id = str(game.get("game_id") or "")
        backup_name = Path(str(payload.get("backup") or "")).name
        if not backup_name:
            raise BadRequest("backup is required.")

        def worker(_cancel_event):
            archive = restore_saves(game, DATA.parent / "save-backups", backup_name)
            return {"restored": archive.name, "game_id": game_id}

        job = JOB_MANAGER.submit(f"saves-restore:{game_id or 'game'}", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def prune_save_history(self, payload):
        """Apply the retention limit to this game's history, oldest first."""
        game = game_from_payload(load_state(), payload)
        raw_keep = payload.get("keep")
        if raw_keep is None:
            settings = load_state().get("settings", {})
            raw_keep = settings.get("save_backup_limit", 10) if isinstance(settings, dict) else 10
        try:
            keep = int(raw_keep)
        except (TypeError, ValueError) as error:
            raise BadRequest("keep must be an integer.") from error
        if keep < 0:
            raise BadRequest("keep must be >= 0.")
        result = apply_retention(game, DATA.parent / "save-backups", keep)
        self.send_json(200, result)

    def add_game_save_path(self, payload):
        path = approved_save_path(payload.get("path", ""))
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
        def worker(_cancel_event):
            state = load_state()
            found = scan_all_saves(state["games"])
            found_by_id = {
                str(state["games"][index].get("game_id")): paths
                for index, paths in found.items()
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
                return updated

            _, updated = transact_state(mutate)
            return {"updated": updated, "games": len(found)}

        job = JOB_MANAGER.submit("saves-scan", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})
