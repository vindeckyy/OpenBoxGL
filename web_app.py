#!/usr/bin/env python3
"""Local browser UI for OpenBox. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC."""

import json
import html
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse

from arcade import import_arcade
from catalog import PROGRESS, bulk_update, related_game_ids
from cloud_sync import sync_statistics
from emulators import emulator_status, install_emulator
from importers import import_heroic, import_lutris, import_steam
from metadata import apply_game_metadata, search_games, sync_database
from openbox import DATA, EXTENSIONS, PLATFORM_BY_EXTENSION, build_launch, discover_profiles, load_state, save_state
from plugins import install_plugin, list_plugins, remove_plugin, run_plugins, set_plugin_enabled
from retroachievements import api_get as ra_api_get, game_progress as ra_game_progress, load_credentials as load_ra_credentials, match_game as match_ra_game, save_credentials as save_ra_credentials
from saves import backup_saves, discover_save_paths, list_backups, restore_saves
from updates import VERSION, check_update, install_desktop_entry, install_update

ROOT = Path(__file__).parent
TOKEN = secrets.token_urlsafe(24)
STATE_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
RUNNING = {}
PROCESSES = {}
SESSION_EVENTS = []
EVENT_SEQUENCE = 0
INSTALLS = {}
METADATA_JOB = {}
MEDIA_JOB = {}
WATCH_STOP = threading.Event()
METADATA_DATABASE = DATA.parent / "metadata/launchbox.db"
FIELDS = {
    "name", "platform", "genre", "year", "developer", "publisher", "series",
    "collection", "description", "path", "launch", "cover", "background",
    "source", "steam_app_id", "lutris_id", "install_dir",
    "heroic_app_id", "rom_name", "clone_of", "set_type", "ra_game_id", "ra_hash", "launchbox_db_id", "archive_member", "video", "music",
    "progress", "rating", "notes", "region", "play_mode", "sort_title", "added_at",
}


def game_identity(game):
    if game.get("steam_app_id"):
        return "steam", str(game["steam_app_id"])
    if game.get("heroic_app_id"):
        return "heroic", str(game.get("source", "")), str(game["heroic_app_id"])
    if game.get("lutris_id"):
        return "lutris", str(game["lutris_id"])
    if game.get("rom_name"):
        return "arcade", str(game.get("source", "")), str(game["rom_name"])
    return "path", str(Path(game.get("path", "")).expanduser())


def import_folder_path(folder):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    found = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in EXTENSIONS)
    with STATE_LOCK:
        state = load_state()
        existing = {game.get("path") for game in state["games"]}
        additions = [{
            "name": path.stem,
            "platform": PLATFORM_BY_EXTENSION.get(path.suffix.lower(), "Imported"),
            "genre": "",
            "path": str(path),
            "added_at": datetime.now().isoformat(timespec="seconds"),
        } for path in found if str(path) not in existing]
        if additions:
            state["games"].extend(additions)
            save_state(state)
    return len(additions), len(found)


def auto_import_worker():
    while not WATCH_STOP.wait(10):
        folders = load_state().get("settings", {}).get("watch_folders", [])
        for folder in folders:
            try:
                import_folder_path(folder)
            except (OSError, ValueError):
                pass


def clean_commands(commands):
    if not isinstance(commands, list) or len(commands) > 25:
        raise ValueError("Application commands must be a list of at most 25 entries.")
    clean = []
    for command in commands:
        command = str(command).strip()
        if command:
            if not shlex.split(command):
                raise ValueError("Application command is empty.")
            clean.append(command)
    return clean


def run_configured_commands(key):
    for command in load_state().get("settings", {}).get(key, []):
        try:
            args = shlex.split(command)
            args[0] = str(Path(args[0]).expanduser())
            subprocess.Popen(args, start_new_session=True)
        except (OSError, ValueError, IndexError):
            pass


def public_settings(state=None):
    settings = (state or load_state()).get("settings", {})
    return {
        "watch_folders": settings.get("watch_folders", []),
        "screensaver_seconds": settings.get("screensaver_seconds", 90),
        "controller_map": settings.get("controller_map", {}),
        "image_group": settings.get("image_group", "cover"),
        "image_group_by_platform": settings.get("image_group_by_platform", {}),
        "image_group_by_playlist": settings.get("image_group_by_playlist", {}),
        "cloud_folder": settings.get("cloud_folder", ""),
        "last_cloud_sync": settings.get("last_cloud_sync", ""),
        "startup_commands": settings.get("startup_commands", []),
        "shutdown_commands": settings.get("shutdown_commands", []),
        "version": VERSION,
        "appimage": bool(os.environ.get("APPIMAGE")),
    }


def public_state():
    with STATE_LOCK:
        state = load_state()
    games = []
    for index, game in enumerate(state["games"]):
        visible = {key: game.get(key, "") for key in FIELDS}
        visible.update({
            "id": index,
            "favorite": bool(game.get("favorite")),
            "hidden": bool(game.get("hidden")),
            "last_played": game.get("last_played", ""),
            "play_count": game.get("play_count", 0),
            "playtime_seconds": game.get("playtime_seconds", 0),
            "path_exists": bool(game.get("path")) and Path(game["path"]).exists(),
            "has_cover": bool(game.get("cover")) and Path(game["cover"]).is_file(),
            "has_background": bool(game.get("background")) and Path(game["background"]).is_file(),
            "has_video": bool(game.get("video")) and Path(game["video"]).is_file(),
            "has_music": bool(game.get("music")) and Path(game["music"]).is_file(),
            "extract_archive": bool(game.get("extract_archive")),
            "applications": game.get("applications", []),
            "versions": game.get("versions", []),
            "documents": game.get("documents", []),
            "save_paths": game.get("save_paths", []),
            "screenshots": game.get("screenshots", []),
            "available_screenshots": [
                index for index, path in enumerate(game.get("screenshots", []))
                if Path(path).is_file()
            ],
        })
        games.append(visible)
    decorated = run_plugins(DATA.parent / "plugins", "library", {"games":games}).get("games", games)
    if isinstance(decorated, list) and len(decorated) == len(games) and all(isinstance(game, dict) for game in decorated):
        games = decorated
        for index, game in enumerate(games):
            game["id"] = index
    return {
        "games": games,
        "playlists": state.get("playlists", []),
        "ra_configured": bool(load_ra_credentials(DATA.parent)),
        "settings": public_settings(state),
    }


def session_event(kind, launch_id, game_name):
    global EVENT_SEQUENCE
    with PROCESS_LOCK:
        EVENT_SEQUENCE += 1
        SESSION_EVENTS.append({
            "id": EVENT_SEQUENCE,
            "kind": kind,
            "launch_id": launch_id,
            "game": game_name,
            "time": datetime.now().isoformat(timespec="seconds"),
        })
        SESSION_EVENTS[:] = SESSION_EVENTS[-100:]


def finish_session(launch_id, game_path, game_name, started, process):
    exit_code = process.wait()
    seconds = max(1, int((datetime.now() - started).total_seconds()))
    with STATE_LOCK:
        state = load_state()
        game = next((item for item in state["games"] if item.get("path") == game_path and item.get("name") == game_name), None)
        if game:
            game["playtime_seconds"] = game.get("playtime_seconds", 0) + seconds
        session = {
            "game": game_name,
            "started": started.isoformat(timespec="seconds"),
            "seconds": seconds,
            "exit_code": exit_code,
        }
        state["history"].append(session)
        state["history"][:] = state["history"][-500:]
        save_state(state)
    with PROCESS_LOCK:
        running = RUNNING.pop(launch_id, {})
        PROCESSES.pop(launch_id, None)
    session_event("stopped", launch_id, game_name)
    run_plugins(DATA.parent / "plugins", "after_session", session)
    try:
        sync_cloud()
    except (OSError, ValueError):
        pass
    if running.get("restart"):
        state = load_state()
        index = next((index for index, game in enumerate(state["games"]) if game.get("path") == game_path and game.get("name") == game_name), None)
        if index is not None:
            try:
                start_game(index)
            except (OSError, ValueError, IndexError):
                pass


def download_image(url, destination):
    request = Request(url, headers={"User-Agent": "OpenBox/1"})
    with urlopen(request, timeout=15) as response:
        if not response.headers.get_content_type().startswith("image/"):
            raise ValueError("The media server did not return an image.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.read())
    return str(destination)


def update_steam_metadata(game):
    app_id = str(game.get("steam_app_id", ""))
    if not app_id.isdigit():
        raise ValueError("This game has no Steam App ID.")
    request = Request(
        f"https://store.steampowered.com/api/appdetails?appids={app_id}",
        headers={"User-Agent": "OpenBox/1"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    record = payload.get(app_id, {})
    if not record.get("success"):
        raise ValueError("Steam did not return metadata for this game.")
    data = record["data"]
    game.update({
        "name": data.get("name") or game.get("name", ""),
        "developer": ", ".join(data.get("developers", [])),
        "publisher": ", ".join(data.get("publishers", [])),
        "genre": ", ".join(item["description"] for item in data.get("genres", [])),
        "year": data.get("release_date", {}).get("date", ""),
        "description": html.unescape(re.sub(r"<[^>]+>", "", data.get("short_description", ""))),
    })
    media = DATA.parent / "media" / "steam" / app_id
    try:
        game["cover"] = download_image(
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900_2x.jpg",
            media / "cover.jpg",
        )
    except (OSError, ValueError):
        pass
    if data.get("header_image"):
        try:
            game["background"] = download_image(data["header_image"], media / "background.jpg")
        except (OSError, ValueError):
            pass


def start_game(index):
    with STATE_LOCK:
        state = load_state()
        game = state["games"][index]
        args, cwd = build_launch(game, state["profiles"])
        result = run_plugins(DATA.parent / "plugins", "before_launch", {"game":game, "args":args, "cwd":cwd})
        args, cwd = result.get("args"), result.get("cwd")
        if not isinstance(args, list) or not args or not all(isinstance(part, str) and part for part in args):
            raise ValueError("A plugin returned an invalid launch command.")
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            raise ValueError("A plugin returned an invalid working directory.")
        process = subprocess.Popen(args, cwd=cwd, start_new_session=True)
        started = datetime.now()
        launch_id = secrets.token_urlsafe(8)
        game["last_played"] = started.isoformat(timespec="seconds")
        game["play_count"] = game.get("play_count", 0) + 1
        if not game.get("progress"):
            game["progress"] = "Playing"
        save_state(state)
        entry = {
            "launch_id": launch_id,
            "game_id": index,
            "game": game.get("name", "Untitled"),
            "started": started.isoformat(timespec="seconds"),
            "pid": process.pid,
            "paused": False,
        }
    with PROCESS_LOCK:
        RUNNING[launch_id] = entry
        PROCESSES[launch_id] = process
    session_event("started", launch_id, entry["game"])
    threading.Thread(
        target=finish_session,
        args=(launch_id, game.get("path", ""), entry["game"], started, process),
        daemon=True,
    ).start()
    return dict(entry)


def control_game_session(launch_id, action):
    with PROCESS_LOCK:
        process = PROCESSES.get(launch_id)
        running = RUNNING.get(launch_id)
        if not process or not running or process.poll() is not None:
            raise ValueError("That game is no longer running.")
        if action == "pause":
            os.killpg(process.pid, signal.SIGSTOP)
            running["paused"] = True
        elif action == "resume":
            os.killpg(process.pid, signal.SIGCONT)
            running["paused"] = False
        elif action in {"stop", "restart", "kill"}:
            running["restart"] = action == "restart"
            if running.get("paused") and action != "kill":
                os.killpg(process.pid, signal.SIGCONT)
            os.killpg(process.pid, signal.SIGKILL if action == "kill" else signal.SIGTERM)
        else:
            raise ValueError("Unknown session action.")
        game = running["game"]
    if action in {"pause", "resume"}:
        session_event("paused" if action == "pause" else "resumed", launch_id, game)
    return {"ok": True, "action": action}


def sync_cloud():
    with STATE_LOCK:
        state = load_state()
        folder = state.get("settings", {}).get("cloud_folder", "")
        if not folder:
            raise ValueError("Configure a mounted cloud sync folder first.")
        result = sync_statistics(state, folder)
        save_state(state)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenBox/1"

    def log_message(self, *_):
        pass

    def headers_common(self, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.headers_common(content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def authorized(self):
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        provided = self.headers.get("X-OpenBox-Token", "") or query_token
        return secrets.compare_digest(provided, TOKEN)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_bytes()
            self.send_bytes(200, html, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/theme.css":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            name = parse_qs(parsed.query).get("name", [""])[0]
            theme = DATA.parent / "themes" / f"{Path(name).stem}.css"
            if not name or not theme.is_file() or theme.stem != name:
                self.send_bytes(200, b"", "text/css; charset=utf-8")
                return
            self.send_bytes(200, theme.read_bytes(), "text/css; charset=utf-8")
            return
        if parsed.path == "/api/library":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, public_state())
            return
        if parsed.path == "/api/profiles":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            state = load_state()
            self.send_json(200, {"profiles": state["profiles"], "detected": discover_profiles()})
            return
        if parsed.path == "/api/settings":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, public_settings())
            return
        if parsed.path == "/api/update":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, check_update())
            return
        if parsed.path == "/api/related":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                index = int(parse_qs(parsed.query)["id"][0])
                games = load_state()["games"]
                related = related_game_ids(games, index)
                self.send_json(200, {"ids": related})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/emulators":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            emulators = emulator_status()
            with PROCESS_LOCK:
                for emulator in emulators:
                    emulator["job"] = INSTALLS.get(emulator["app_id"], {})
            self.send_json(200, {"emulators": emulators})
            return
        if parsed.path == "/api/saves":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                game = load_state()["games"][int(parse_qs(parsed.query)["id"][0])]
                backups = [{"name": path.name, "size": path.stat().st_size} for path in list_backups(game, DATA.parent / "save-backups")]
                self.send_json(200, {"backups": backups})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/saves/discover":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                game = load_state()["games"][int(parse_qs(parsed.query)["id"][0])]
                configured = set(game.get("save_paths", []))
                candidates = [item for item in discover_save_paths(game) if item["path"] not in configured]
                self.send_json(200, {"candidates":candidates})
            except (KeyError, IndexError, ValueError):
                self.send_json(404, {"error": "Game not found"})
            return
        if parsed.path == "/api/themes":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            themes = sorted(path.stem for path in (DATA.parent / "themes").glob("*.css"))
            settings = load_state().get("settings", {})
            platform = parse_qs(parsed.query).get("platform", [""])[0]
            mappings = settings.get("theme_by_platform", {})
            self.send_json(200, {
                "themes":themes,
                "selected":mappings.get(platform, settings.get("theme", "")) if platform else settings.get("theme", ""),
                "global":settings.get("theme", ""),
                "mappings":mappings,
            })
            return
        if parsed.path == "/api/running":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            try:
                after = int(parse_qs(parsed.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            with PROCESS_LOCK:
                payload = {
                    "running": list(RUNNING.values()),
                    "events": [event for event in SESSION_EVENTS if event["id"] > after],
                    "last_event": EVENT_SEQUENCE,
                }
            self.send_json(200, payload)
            return
        if parsed.path == "/api/ra/settings":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
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
        if parsed.path == "/api/plugins":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            self.send_json(200, {"plugins":list_plugins(DATA.parent / "plugins")})
            return
        if parsed.path == "/api/metadata/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            with PROCESS_LOCK:
                job = dict(METADATA_JOB)
            self.send_json(200, {"ready":METADATA_DATABASE.is_file(), "job":job})
            return
        if parsed.path == "/api/metadata/search":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            if not METADATA_DATABASE.is_file():
                self.send_json(409, {"error": "Download the LaunchBox metadata database first."})
                return
            try:
                query = parse_qs(parsed.query)
                game = load_state()["games"][int(query["id"][0])]
                title = query.get("q", [game.get("name", "")])[0]
                results = search_games(METADATA_DATABASE, title, game.get("platform", ""))
                self.send_json(200, {"results":results})
            except (KeyError, IndexError, ValueError, sqlite3.Error) as error:
                self.send_json(400, {"error":str(error)})
            return
        if parsed.path == "/api/media/audit":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            platform = query.get("platform", ["all"])[0]
            games = [
                game for game in load_state()["games"]
                if platform == "all" or game.get("platform") == platform
            ]
            self.send_json(200, {
                "games":len(games),
                "matched":sum(bool(game.get("launchbox_db_id")) for game in games),
                "missing_cover":sum(not Path(game.get("cover", "")).is_file() for game in games),
                "missing_background":sum(not Path(game.get("background", "")).is_file() for game in games),
                "missing_screenshots":sum(not any(Path(path).is_file() for path in game.get("screenshots", [])) for game in games),
            })
            return
        if parsed.path == "/api/media/bulk/status":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            with PROCESS_LOCK:
                job = dict(MEDIA_JOB)
            self.send_json(200, {"job":job})
            return
        if parsed.path == "/api/ra/badge":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            name = re.sub(r"[^A-Za-z0-9_-]", "", query.get("name", [""])[0])
            locked = query.get("locked", ["0"])[0] == "1"
            if not name:
                self.send_json(404, {"error": "Badge not found"})
                return
            badge = DATA.parent / "media/retroachievements/badges" / f"{name}{'_lock' if locked else ''}.png"
            try:
                if not badge.is_file():
                    download_image(f"https://media.retroachievements.org/Badge/{badge.name}", badge)
                self.send_bytes(200, badge.read_bytes(), "image/png")
            except (OSError, ValueError):
                self.send_json(404, {"error": "Badge not found"})
            return
        if parsed.path == "/api/media":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            try:
                game = load_state()["games"][int(query["id"][0])]
                kind = query["kind"][0]
                if kind == "screenshot":
                    index = int(query["index"][0])
                    media = Path(game.get("screenshots", [])[index])
                elif kind in {"cover", "background", "video", "music"}:
                    media = Path(game.get(kind, ""))
                else:
                    raise ValueError
                if not media.is_file():
                    raise FileNotFoundError
                self.send_bytes(200, media.read_bytes(), mimetypes.guess_type(media.name)[0] or "application/octet-stream")
            except (KeyError, IndexError, ValueError, FileNotFoundError):
                self.send_json(404, {"error": "Media not found"})
            return
        if parsed.path == "/api/document":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            query = parse_qs(parsed.query)
            try:
                game = load_state()["games"][int(query["id"][0])]
                document = game.get("documents", [])[int(query["index"][0])]
                path = Path(document["path"])
                if not path.is_file():
                    raise FileNotFoundError
                self.send_response(200)
                self.headers_common(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                safe_name = re.sub(r'[\r\n"]', "_", path.name)
                self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)
            except (KeyError, IndexError, ValueError, FileNotFoundError):
                self.send_json(404, {"error": "Document not found"})
            return
        if parsed.path == "/api/backup":
            if not self.authorized():
                self.send_json(403, {"error": "Unauthorized"})
                return
            data = json.dumps(load_state(), indent=2).encode()
            self.send_response(200)
            self.headers_common("application/json")
            self.send_header("Content-Disposition", "attachment; filename=openbox-library.json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.authorized():
            self.send_json(403, {"error": "Unauthorized"})
            return
        try:
            payload = self.body()
            route = urlparse(self.path).path
            if route == "/api/launch":
                self.launch(payload)
            elif route == "/api/session/control":
                self.control_session(payload)
            elif route == "/api/favorite":
                self.favorite(payload)
            elif route == "/api/game":
                self.save_game(payload)
            elif route == "/api/game/delete":
                self.delete_game(payload)
            elif route == "/api/games/bulk":
                self.bulk_edit(payload)
            elif route == "/api/import":
                self.import_folder(payload)
            elif route == "/api/import/watch":
                self.scan_watch_folders()
            elif route == "/api/import/steam":
                self.import_steam_games()
            elif route == "/api/import/heroic":
                self.import_heroic_games()
            elif route == "/api/import/lutris":
                self.import_lutris_games()
            elif route == "/api/import/arcade":
                self.import_arcade_games(payload)
            elif route == "/api/metadata/steam":
                self.steam_metadata(payload)
            elif route == "/api/metadata/sync":
                self.sync_metadata()
            elif route == "/api/metadata/apply":
                self.apply_metadata(payload)
            elif route == "/api/media/bulk":
                self.bulk_media(payload)
            elif route == "/api/profiles":
                self.save_profiles(payload)
            elif route == "/api/settings":
                self.save_settings(payload)
            elif route == "/api/image-group":
                self.save_image_group(payload)
            elif route == "/api/cloud/sync":
                self.send_json(200, sync_cloud())
            elif route == "/api/update/install":
                update = check_update()
                self.send_json(200, install_update(update))
            elif route == "/api/desktop/install":
                self.send_json(200, {"desktop": install_desktop_entry()})
            elif route == "/api/emulators/install":
                self.install_emulator(payload)
            elif route == "/api/ra/settings":
                self.save_ra_settings(payload)
            elif route == "/api/ra/game":
                self.ra_game(payload)
            elif route == "/api/plugins/install":
                self.install_plugin(payload)
            elif route == "/api/plugins/toggle":
                self.toggle_plugin(payload)
            elif route == "/api/plugins/remove":
                self.remove_plugin(payload)
            elif route == "/api/extra/launch":
                self.launch_extra(payload)
            elif route == "/api/saves/backup":
                self.backup_game_saves(payload)
            elif route == "/api/saves/restore":
                self.restore_game_saves(payload)
            elif route == "/api/saves/add":
                self.add_game_save_path(payload)
            elif route == "/api/themes/select":
                self.select_theme(payload)
            elif route == "/api/themes/import":
                self.import_theme(payload)
            elif route == "/api/playlists":
                self.save_playlist(payload)
            elif route == "/api/playlists/delete":
                self.delete_playlist(payload)
            elif route == "/api/health":
                self.health()
            elif route == "/api/health/dedupe":
                self.dedupe()
            else:
                self.send_json(404, {"error": "Not found"})
        except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})

    def launch(self, payload):
        self.send_json(200, {"ok": True, **start_game(int(payload["id"]))})

    def control_session(self, payload):
        launch_id = str(payload.get("launch_id", ""))
        action = str(payload.get("action", ""))
        self.send_json(200, control_game_session(launch_id, action))

    def favorite(self, payload):
        with STATE_LOCK:
            state = load_state()
            game = state["games"][int(payload["id"])]
            game["favorite"] = not game.get("favorite", False)
            save_state(state)
        self.send_json(200, {"favorite": game["favorite"]})

    def save_game(self, payload):
        source = payload.get("game", {})
        game = {key: str(source[key]).strip() for key in FIELDS if key in source}
        game["extract_archive"] = bool(source.get("extract_archive"))
        game["hidden"] = bool(source.get("hidden"))
        if game.get("progress", "") not in PROGRESS:
            raise ValueError("Unknown progress value.")
        try:
            game["rating"] = float(game.get("rating") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("Rating must be a number from 0 to 5.") from error
        if not 0 <= game["rating"] <= 5:
            raise ValueError("Rating must be between 0 and 5.")
        game["applications"] = self.clean_extras(source.get("applications", []), command=True)
        game["versions"] = self.clean_extras(source.get("versions", []), command=True)
        game["documents"] = self.clean_extras(source.get("documents", []), command=False)
        save_paths = source.get("save_paths", [])
        if not isinstance(save_paths, list):
            raise ValueError("Save paths must be a list.")
        game["save_paths"] = [str(path).strip() for path in save_paths if str(path).strip()][:50]
        screenshots = source.get("screenshots", [])
        if not isinstance(screenshots, list):
            raise ValueError("Screenshots must be a list.")
        game["screenshots"] = [str(path).strip() for path in screenshots if str(path).strip()][:100]
        if not game.get("name"):
            raise ValueError("Name is required.")
        if not game.get("path") or not Path(game["path"]).exists():
            raise ValueError("Path must point to an existing local file.")
        with STATE_LOCK:
            state = load_state()
            if payload.get("id") is None:
                game["added_at"] = datetime.now().isoformat(timespec="seconds")
                state["games"].append(game)
            else:
                existing = state["games"][int(payload["id"])]
                existing.update(game)
            save_state(state)
        self.send_json(200, {"ok": True})

    def bulk_edit(self, payload):
        with STATE_LOCK:
            state = load_state()
            changed = bulk_update(state["games"], payload.get("ids"), payload.get("changes"))
            save_state(state)
        self.send_json(200, {"updated": changed})

    def delete_game(self, payload):
        with STATE_LOCK:
            state = load_state()
            game = state["games"].pop(int(payload["id"]))
            save_state(state)
        self.send_json(200, {"removed":game.get("name", "")})

    @staticmethod
    def clean_extras(items, command):
        if not isinstance(items, list):
            raise ValueError("Game extras must be a list.")
        clean = []
        for item in items[:100]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            record = {"name": str(item.get("name") or Path(path).stem).strip(), "path": path}
            if command:
                record["command"] = str(item.get("command", "")).strip()
            clean.append(record)
        return clean

    def import_folder(self, payload):
        added, found = import_folder_path(str(payload.get("folder", "")))
        self.send_json(200, {"added": added, "found": found})

    def scan_watch_folders(self):
        folders = load_state().get("settings", {}).get("watch_folders", [])
        added = found = 0
        errors = []
        for folder in folders:
            try:
                folder_added, folder_found = import_folder_path(folder)
                added += folder_added
                found += folder_found
            except (OSError, ValueError) as error:
                errors.append(str(error))
        self.send_json(200, {"added": added, "found": found, "errors": errors})

    def import_steam_games(self):
        imported = import_steam()
        with STATE_LOCK:
            state = load_state()
            existing = {str(game.get("steam_app_id")) for game in state["games"] if game.get("steam_app_id")}
            new_games = [game for game in imported if game["steam_app_id"] not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        self.send_json(200, {"added": len(new_games), "found": len(imported)})

    def import_heroic_games(self):
        imported = import_heroic()
        with STATE_LOCK:
            state = load_state()
            existing = {
                (game.get("source"), str(game.get("heroic_app_id")))
                for game in state["games"] if game.get("heroic_app_id")
            }
            new_games = [game for game in imported if (game["source"], game["heroic_app_id"]) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        self.send_json(200, {"added": len(new_games), "found": len(imported)})

    def import_lutris_games(self):
        imported = import_lutris()
        with STATE_LOCK:
            state = load_state()
            existing = {str(game.get("lutris_id")) for game in state["games"] if game.get("lutris_id")}
            new_games = [game for game in imported if game["lutris_id"] not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        self.send_json(200, {"added": len(new_games), "found": len(imported)})

    def import_arcade_games(self, payload):
        imported = import_arcade(
            str(payload.get("folder", "")),
            str(payload.get("dat", "")),
            str(payload.get("command", "")),
            str(payload.get("source", "MAME")),
        )
        with STATE_LOCK:
            state = load_state()
            existing = {
                (game.get("source"), str(game.get("rom_name")))
                for game in state["games"] if game.get("rom_name")
            }
            new_games = [game for game in imported if (game["source"], game["rom_name"]) not in existing]
            timestamp = datetime.now().isoformat(timespec="seconds")
            for game in new_games:
                game["added_at"] = timestamp
            state["games"].extend(new_games)
            save_state(state)
        counts = {kind: sum(game["set_type"] == kind for game in imported) for kind in ("parent", "merged", "split", "non-merged")}
        self.send_json(200, {"added": len(new_games), "found": len(imported), "sets": counts})

    def steam_metadata(self, payload):
        with STATE_LOCK:
            state = load_state()
            game = state["games"][int(payload["id"])]
            update_steam_metadata(game)
            save_state(state)
        self.send_json(200, {"ok": True})

    def sync_metadata(self):
        with PROCESS_LOCK:
            if METADATA_JOB.get("state") == "downloading":
                self.send_json(200, METADATA_JOB)
                return
            METADATA_JOB.clear()
            METADATA_JOB.update({"state":"downloading"})

        def worker():
            try:
                sync_database(METADATA_DATABASE)
                job = {"state":"done"}
            except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error) as error:
                job = {"state":"error", "error":str(error)}
            with PROCESS_LOCK:
                METADATA_JOB.clear()
                METADATA_JOB.update(job)

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state":"downloading"})

    def apply_metadata(self, payload):
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        index = int(payload["id"])
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not set(media_types) <= {"cover", "background", "screenshots"}:
            raise ValueError("Invalid media selection.")
        state = load_state()
        original = dict(state["games"][index])
        updated = apply_game_metadata(
            dict(original), METADATA_DATABASE, int(payload["database_id"]), media_types,
            DATA.parent / "media/launchbox", bool(payload.get("overwrite")),
        )
        changes = {key:value for key,value in updated.items() if original.get(key) != value}
        with STATE_LOCK:
            state = load_state()
            state["games"][index].update(changes)
            save_state(state)
        self.send_json(200, {"updated":sorted(changes)})

    def bulk_media(self, payload):
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not media_types or not set(media_types) <= {"cover", "background", "screenshots"}:
            raise ValueError("Select at least one valid media type.")
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        platform = str(payload.get("platform", "all"))
        overwrite = bool(payload.get("overwrite"))
        with PROCESS_LOCK:
            if MEDIA_JOB.get("state") == "running":
                self.send_json(200, MEDIA_JOB)
                return
            MEDIA_JOB.clear()
            MEDIA_JOB.update({"state":"running", "current":0, "total":0, "updated":0, "errors":[]})

        def worker():
            state = load_state()
            targets = [
                index for index, game in enumerate(state["games"])
                if game.get("launchbox_db_id") and (platform == "all" or game.get("platform") == platform)
            ]
            with PROCESS_LOCK:
                MEDIA_JOB["total"] = len(targets)
            updated_count, errors = 0, []
            for current, index in enumerate(targets, 1):
                original = {}
                try:
                    state = load_state()
                    original = dict(state["games"][index])
                    updated = apply_game_metadata(
                        dict(original), METADATA_DATABASE, int(original["launchbox_db_id"]), media_types,
                        DATA.parent / "media/launchbox", overwrite,
                    )
                    changes = {key:value for key,value in updated.items() if original.get(key) != value}
                    if changes:
                        with STATE_LOCK:
                            state = load_state()
                            state["games"][index].update(changes)
                            save_state(state)
                        updated_count += 1
                except (OSError, ValueError, sqlite3.Error) as error:
                    errors.append(f"{original.get('name', index)}: {error}")
                with PROCESS_LOCK:
                    MEDIA_JOB.update({"current":current, "updated":updated_count, "errors":errors[-20:]})
            with PROCESS_LOCK:
                MEDIA_JOB["state"] = "done"

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state":"running"})

    def save_profiles(self, payload):
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict):
            raise ValueError("Profiles must be an object.")
        clean = {
            str(platform).strip(): str(command).strip()
            for platform, command in profiles.items()
            if str(platform).strip() and str(command).strip()
        }
        with STATE_LOCK:
            state = load_state()
            state["profiles"] = clean
            save_state(state)
        self.send_json(200, {"saved": len(clean)})

    def save_settings(self, payload):
        folders = payload.get("watch_folders", [])
        if not isinstance(folders, list) or len(folders) > 50:
            raise ValueError("Watch folders must be a list of at most 50 paths.")
        clean_folders = []
        for value in folders:
            path = Path(str(value)).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f"Watch folder does not exist: {path}")
            if str(path) not in clean_folders:
                clean_folders.append(str(path))
        seconds = int(payload.get("screensaver_seconds", 90))
        if seconds and not 30 <= seconds <= 3600:
            raise ValueError("Screensaver delay must be 0 or between 30 and 3600 seconds.")
        mapping = payload.get("controller_map", {})
        if not isinstance(mapping, dict):
            raise ValueError("Controller mapping must be an object.")
        allowed = {"play", "back", "favorite", "random", "page_left", "page_right", "pause", "menu"}
        clean_mapping = {}
        for action, button in mapping.items():
            if action not in allowed or not isinstance(button, int) or not 0 <= button <= 31:
                raise ValueError("Controller button mappings must use buttons 0 through 31.")
            clean_mapping[action] = button
        cloud_folder = str(payload.get("cloud_folder", "")).strip()
        if cloud_folder:
            cloud_path = Path(cloud_folder).expanduser()
            if not cloud_path.is_absolute() or not cloud_path.is_dir():
                raise ValueError(f"Cloud sync folder does not exist: {cloud_path}")
            cloud_folder = str(cloud_path)
        startup_commands = clean_commands(payload.get("startup_commands", []))
        shutdown_commands = clean_commands(payload.get("shutdown_commands", []))
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            settings.update({
                "watch_folders": clean_folders,
                "screensaver_seconds": seconds,
                "controller_map": clean_mapping,
                "cloud_folder": cloud_folder,
                "startup_commands": startup_commands,
                "shutdown_commands": shutdown_commands,
            })
            save_state(state)
        self.send_json(200, public_settings(state))

    def save_image_group(self, payload):
        group = str(payload.get("group", ""))
        scope = str(payload.get("scope", "global"))
        name = str(payload.get("name", "")).strip()
        if group not in {"default", "cover", "background", "screenshot"} or scope not in {"global", "platform", "playlist"}:
            raise ValueError("Unknown image group.")
        if scope != "global" and (not name or len(name) > 200):
            raise ValueError("A platform or playlist is required.")
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            if scope == "global":
                settings["image_group"] = "cover" if group == "default" else group
            else:
                mappings = settings.setdefault(f"image_group_by_{scope}", {})
                if group == "default":
                    mappings.pop(name, None)
                else:
                    mappings[name] = group
            save_state(state)
        self.send_json(200, public_settings(state))

    def install_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
        with PROCESS_LOCK:
            if INSTALLS.get(app_id, {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS[app_id] = {"state": "installing"}

        def worker():
            try:
                profiles = install_emulator(app_id)
                with STATE_LOCK:
                    state = load_state()
                    for platform, command in profiles.items():
                        state["profiles"].setdefault(platform, command)
                    save_state(state)
                job = {"state": "done", "profiles": profiles}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[app_id] = job

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"state": "installing"})

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
        index = int(payload["id"])
        state = load_state()
        game = state["games"][index]
        game_id, digest = match_ra_game(game, credentials, DATA.parent / "cache/retroachievements")
        with STATE_LOCK:
            state = load_state()
            state["games"][index]["ra_game_id"] = str(game_id)
            state["games"][index]["ra_hash"] = digest
            save_state(state)
        progress = ra_game_progress(game_id, credentials)
        progress["game_id"] = game_id
        self.send_json(200, progress)

    def install_plugin(self, payload):
        manifest = install_plugin(str(payload.get("path", "")), DATA.parent / "plugins")
        self.send_json(200, {"plugin":manifest})

    def toggle_plugin(self, payload):
        enabled = set_plugin_enabled(
            DATA.parent / "plugins",
            str(payload.get("id", "")),
            bool(payload.get("enabled")),
        )
        self.send_json(200, {"enabled":enabled})

    def remove_plugin(self, payload):
        plugin_id = remove_plugin(DATA.parent / "plugins", str(payload.get("id", "")))
        self.send_json(200, {"removed":plugin_id})

    def launch_extra(self, payload):
        state = load_state()
        game = state["games"][int(payload["id"])]
        kind = payload.get("kind")
        if kind not in {"applications", "versions", "documents"}:
            raise ValueError("Unknown extra type.")
        extra = game.get(kind, [])[int(payload["index"])]
        path = Path(extra["path"])
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        if kind == "documents":
            opener = shutil.which("xdg-open")
            if not opener:
                raise FileNotFoundError("xdg-open is required to open documents.")
            args = [opener, str(path)]
        elif extra.get("command"):
            args = [part.replace("{path}", str(path)) for part in shlex.split(extra["command"])]
        else:
            args = [str(path)]
        subprocess.Popen(args, cwd=str(path.parent))
        self.send_json(200, {"ok": True})

    def backup_game_saves(self, payload):
        game = load_state()["games"][int(payload["id"])]
        archive = backup_saves(game, DATA.parent / "save-backups")
        self.send_json(200, {"backup": archive.name})

    def restore_game_saves(self, payload):
        game = load_state()["games"][int(payload["id"])]
        archive = restore_saves(game, DATA.parent / "save-backups", str(payload["backup"]))
        self.send_json(200, {"restored": archive.name})

    def add_game_save_path(self, payload):
        path = Path(str(payload.get("path", ""))).expanduser()
        if not path.exists():
            raise FileNotFoundError("Save path does not exist.")
        with STATE_LOCK:
            state = load_state()
            paths = state["games"][int(payload["id"])].setdefault("save_paths", [])
            if str(path) not in paths:
                paths.append(str(path))
            save_state(state)
        self.send_json(200, {"path":str(path)})

    def select_theme(self, payload):
        name = str(payload.get("name", "")).strip()
        platform = str(payload.get("platform", "")).strip()
        if name and not (DATA.parent / "themes" / f"{Path(name).stem}.css").is_file():
            raise FileNotFoundError("Theme not found.")
        with STATE_LOCK:
            state = load_state()
            settings = state.setdefault("settings", {})
            if platform:
                mappings = settings.setdefault("theme_by_platform", {})
                if name:
                    mappings[platform] = name
                else:
                    mappings.pop(platform, None)
            else:
                settings["theme"] = name
            save_state(state)
        self.send_json(200, {"selected":name, "platform":platform})

    def import_theme(self, payload):
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file() or source.suffix.lower() != ".css":
            raise ValueError("Theme path must point to a CSS file.")
        destination = DATA.parent / "themes" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self.send_json(200, {"theme": destination.stem})

    def save_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        rules = payload.get("rules", {})
        if not name or not isinstance(rules, dict):
            raise ValueError("Playlist name and rules are required.")
        clean = {
            key: str(rules.get(key, "")).strip()
            for key in ("platform", "view", "query")
            if str(rules.get(key, "")).strip()
        }
        with STATE_LOCK:
            state = load_state()
            playlists = state.setdefault("playlists", [])
            existing = next((item for item in playlists if item.get("name") == name), None)
            if existing:
                existing["rules"] = clean
            else:
                playlists.append({"name": name, "rules": clean})
            save_state(state)
        self.send_json(200, {"saved": name})

    def delete_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        with STATE_LOCK:
            state = load_state()
            state["playlists"] = [item for item in state.get("playlists", []) if item.get("name") != name]
            save_state(state)
        self.send_json(200, {"deleted": name})

    def health(self):
        state = load_state()
        seen, duplicates, issues = {}, [], []
        for index, game in enumerate(state["games"]):
            identity = game_identity(game)
            if identity in seen:
                duplicates.append(index)
                issues.append({"id":index, "game":game.get("name", ""), "type":"Duplicate", "detail":f"Matches {state['games'][seen[identity]].get('name', '')}"})
            else:
                seen[identity] = index
            path = Path(game.get("path", ""))
            if not game.get("path") or not path.exists():
                issues.append({"id":index, "game":game.get("name", ""), "type":"Missing game", "detail":str(path)})
            if not Path(game.get("cover", "")).is_file():
                issues.append({"id":index, "game":game.get("name", ""), "type":"Missing box front", "detail":"No local cover image"})
            for kind in ("applications", "versions", "documents"):
                for extra in game.get(kind, []):
                    if not Path(extra.get("path", "")).exists():
                        issues.append({"id":index, "game":game.get("name", ""), "type":"Missing extra", "detail":extra.get("path", "")})
            for path in game.get("save_paths", []):
                if not Path(path).exists():
                    issues.append({"id":index, "game":game.get("name", ""), "type":"Missing save path", "detail":path})
            suffix = Path(game.get("path", "")).suffix.casefold()
            if suffix in {".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".iso"} and not game.get("launch") and not state["profiles"].get(game.get("platform", "")):
                issues.append({"id":index, "game":game.get("name", ""), "type":"No emulator", "detail":game.get("platform", "Unspecified")})
        self.send_json(200, {
            "games": len(state["games"]),
            "missing": sum(issue["type"] == "Missing game" for issue in issues),
            "duplicates": len(duplicates),
            "unconfigured": sum(not game.get("path") for game in state["games"]),
            "missing_media": sum(issue["type"] == "Missing box front" for issue in issues),
            "issues":issues,
        })

    def dedupe(self):
        with STATE_LOCK:
            state = load_state()
            seen, kept, removed = set(), [], []
            for game in state["games"]:
                identity = game_identity(game)
                if identity in seen:
                    removed.append(game.get("name", ""))
                else:
                    seen.add(identity)
                    kept.append(game)
            state["games"] = kept
            save_state(state)
        self.send_json(200, {"removed":removed})


def main():
    WATCH_STOP.clear()
    threading.Thread(target=auto_import_worker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    run_configured_commands("startup_commands")
    url = f"http://127.0.0.1:{server.server_port}/?token={TOKEN}"
    print(url, flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WATCH_STOP.set()
        server.server_close()
        run_configured_commands("shutdown_commands")


if __name__ == "__main__":
    main()
