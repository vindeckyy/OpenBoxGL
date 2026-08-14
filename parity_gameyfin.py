"""Gameyfin self-hosted library client for OpenBox."""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from backend_io import atomic_copy_stream, fsync_directory, read_limited

DEFAULT_PROVIDER = "org.gameyfin.plugins.download.direct.DirectDownloadPlugin$DirectDownloadProvider"


class GameyfinError(ValueError):
    pass


def _slug(value):
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "game").casefold()).strip("-")
    return text or "game"


def normalize_base_url(url):
    value = str(url or "").strip().rstrip("/")
    if not value:
        raise GameyfinError("Gameyfin URL is required.")
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value


def gameyfin_settings(settings):
    settings = settings or {}
    return {
        "url": str(settings.get("gameyfin_url", "")).strip(),
        "username": str(settings.get("gameyfin_username", "")).strip(),
        "password": str(settings.get("gameyfin_password", "")),
        "install_dir": str(settings.get("gameyfin_install_dir", "")).strip(),
        "provider": str(settings.get("gameyfin_provider", "")).strip() or DEFAULT_PROVIDER,
        "auto_import": bool(settings.get("storefront_auto_import", {}).get("gameyfin")),
    }


class GameyfinClient:
    def __init__(self, base_url, username="", password="", opener=None):
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.jar = CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._logged_in = False

    def request(self, method, path, data=None, headers=None, raw=False):
        url = path if str(path).startswith("http") else f"{self.base_url}{path}"
        body = None
        request_headers = {"User-Agent": "OpenBox/Gameyfin", "Accept": "application/json, */*"}
        if headers:
            request_headers.update(headers)
        if data is not None and not isinstance(data, (bytes, bytearray)):
            body = json.dumps(data).encode()
            request_headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, (bytes, bytearray)):
            body = data
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise GameyfinError(f"Gameyfin request failed ({error.code}): {detail or error.reason}") from error
        except urllib.error.URLError as error:
            raise GameyfinError(f"Could not reach Gameyfin: {error.reason}") from error
        with response:
            content_type = response.headers.get("Content-Type", "")
            payload = read_limited(response, 16 * 1024 * 1024)
        if raw:
            return response, payload
        if "json" in content_type or payload[:1] in (b"{", b"["):
            try:
                return json.loads(payload.decode() or "null")
            except json.JSONDecodeError as error:
                raise GameyfinError("Gameyfin returned invalid JSON.") from error
        return payload.decode(errors="replace")

    def login(self):
        if not self.username:
            self._logged_in = True
            return False
        body = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
        }).encode()
        self.request(
            "POST",
            "/login",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html,application/json"},
            raw=True,
        )
        self._logged_in = True
        return True

    def ensure_session(self):
        if not self._logged_in:
            self.login()

    def connect(self, endpoint, method, payload=None):
        self.ensure_session()
        path = f"/connect/{endpoint}/{method}"
        try:
            return self.request("POST", path, data=payload if payload is not None else {})
        except GameyfinError:
            if self.username and not self._logged_in:
                raise
            # Retry once after forced login when anonymous access is closed.
            self._logged_in = False
            self.login()
            return self.request("POST", path, data=payload if payload is not None else {})

    def list_games(self):
        result = self.connect("GameEndpoint", "getAll")
        if not isinstance(result, list):
            raise GameyfinError("Gameyfin GameEndpoint.getAll returned an unexpected payload.")
        return result

    def list_providers(self):
        try:
            result = self.connect("DownloadProviderEndpoint", "getProviders")
        except GameyfinError:
            return [{"key": DEFAULT_PROVIDER, "name": "Direct Download", "priority": 1}]
        if not isinstance(result, list):
            return [{"key": DEFAULT_PROVIDER, "name": "Direct Download", "priority": 1}]
        providers = [item for item in result if isinstance(item, dict)]
        if not providers:
            return [{"key": DEFAULT_PROVIDER, "name": "Direct Download", "priority": 1}]
        return providers

    def download_game(self, game_id, provider, destination):
        self.ensure_session()
        provider = provider or DEFAULT_PROVIDER
        query = urllib.parse.urlencode({"provider": provider})
        url = f"{self.base_url}/download/{int(game_id)}?{query}"
        request_headers = {
            "User-Agent": "OpenBox/Gameyfin",
            "Accept": "application/octet-stream,*/*",
        }
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            response = self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise GameyfinError(f"Gameyfin request failed ({error.code}): {detail or error.reason}") from error
        except urllib.error.URLError as error:
            raise GameyfinError(f"Could not reach Gameyfin: {error.reason}") from error
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.I)
        filename = urllib.parse.unquote(match.group(1)) if match else f"gameyfin-{game_id}.bin"
        filename = Path(filename).name or f"gameyfin-{game_id}.bin"
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / filename
        with response:
            atomic_copy_stream(
                response, target, mode=0o600,
                max_bytes=4 * 1024 * 1024 * 1024,
            )
        fsync_directory(destination)
        return target


def platform_from_gameyfin(platforms):
    if not platforms:
        return "PC"
    first = platforms[0]
    if isinstance(first, dict):
        name = str(first.get("name") or first.get("displayName") or first.get("id") or "PC")
    else:
        name = str(first)
    return name.replace("_", " ").title() if name.isupper() or "_" in name else name


def game_from_gameyfin(record, install_root, provider=""):
    game_id = record.get("id")
    title = str(record.get("title") or f"Gameyfin {game_id}")
    platforms = record.get("platforms") or []
    platform = platform_from_gameyfin(platforms)
    folder = Path(install_root).expanduser() / f"{_slug(title)}-{game_id}"
    installed = folder.exists() and any(folder.iterdir()) if folder.is_dir() else folder.is_file()
    path = str(folder) if installed else str(folder)
    cover = ""
    cover_info = record.get("cover") or {}
    if isinstance(cover_info, dict) and cover_info.get("id"):
        cover = f"cover:{cover_info['id']}"
    return {
        "name": title,
        "platform": platform,
        "source": "Gameyfin",
        "collection": "Gameyfin",
        "description": str(record.get("summary") or ""),
        "developer": ", ".join(record.get("developers") or []) if isinstance(record.get("developers"), list) else "",
        "publisher": ", ".join(record.get("publishers") or []) if isinstance(record.get("publishers"), list) else "",
        "year": str(record.get("release") or "")[:4],
        "path": path,
        "launch": path if installed else "",
        "install_dir": str(folder),
        "gameyfin_id": str(game_id),
        "gameyfin_provider": provider,
        "store_catalog": True,
        "store_installed": installed,
        "owned": True,
        "notes": "" if installed else "Owned on Gameyfin. Install to download locally.",
        "cover": cover,
    }


def _resolve_provider(client, conf):
    providers = client.list_providers()
    provider = conf["provider"]
    if provider not in {item.get("key") for item in providers if isinstance(item, dict)}:
        provider = providers[0].get("key", DEFAULT_PROVIDER) if providers else DEFAULT_PROVIDER
    return provider


def catalog_gameyfin(settings, client=None):
    conf = gameyfin_settings(settings)
    if not conf["url"]:
        raise GameyfinError("Configure Gameyfin URL in Settings or Storefronts first.")
    install_root = conf["install_dir"] or str(Path.home() / "Games" / "Gameyfin")
    client = client or GameyfinClient(conf["url"], conf["username"], conf["password"])
    providers = client.list_providers()
    provider = _resolve_provider(client, conf)
    catalog = []
    for record in client.list_games():
        if not isinstance(record, dict) or record.get("id") is None:
            continue
        entry = game_from_gameyfin(record, install_root, provider)
        catalog.append({
            "id": entry["gameyfin_id"],
            "name": entry["name"],
            "source": "Gameyfin",
            "installed": entry["store_installed"],
            "install_uri": f"gameyfin://install/{entry['gameyfin_id']}",
            "path": entry["path"],
            "launch": entry["launch"] or entry["path"],
            "install_dir": entry["install_dir"],
            "gameyfin_id": entry["gameyfin_id"],
            "gameyfin_provider": provider,
            "description": entry.get("description", ""),
            "platform": entry.get("platform", "PC"),
        })
    return catalog, providers


def install_gameyfin_game(settings, game_id, client=None):
    conf = gameyfin_settings(settings)
    if not conf["url"]:
        raise GameyfinError("Configure Gameyfin URL first.")
    install_root = Path(conf["install_dir"] or Path.home() / "Games" / "Gameyfin").expanduser()
    install_root.mkdir(parents=True, exist_ok=True)
    client = client or GameyfinClient(conf["url"], conf["username"], conf["password"])
    records = {str(item.get("id")): item for item in client.list_games() if isinstance(item, dict)}
    record = records.get(str(game_id))
    if not record:
        raise GameyfinError(f"Gameyfin game {game_id} was not found.")
    provider = _resolve_provider(client, conf)
    entry = game_from_gameyfin(record, install_root, provider)
    destination = Path(entry["install_dir"])
    staging = destination.with_name(f".{destination.name}.openbox-installing")
    previous = destination.with_name(f".{destination.name}.openbox-previous")
    if any(path.is_symlink() for path in (install_root, destination, staging, previous)):
        raise GameyfinError("Gameyfin install paths may not be symlinks.")
    if staging.exists():
        shutil.rmtree(staging) if staging.is_dir() else staging.unlink()
    if previous.exists():
        shutil.rmtree(previous) if previous.is_dir() else previous.unlink()
    staging.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = client.download_game(game_id, provider, staging)
        if destination.exists():
            destination.rename(previous)
        staging.rename(destination)
        if previous.exists():
            shutil.rmtree(previous) if previous.is_dir() else previous.unlink()
    except Exception:
        if staging.exists():
            shutil.rmtree(staging) if staging.is_dir() else staging.unlink()
        if not destination.exists() and previous.exists():
            previous.rename(destination)
        raise
    # Prefer launching a single downloaded file; otherwise point at the folder.
    downloaded_name = Path(downloaded).name
    launch_candidate = destination / downloaded_name
    launch_path = launch_candidate if launch_candidate.is_file() else destination
    return {
        **entry,
        "path": str(launch_path),
        "launch": str(launch_path),
        "store_installed": True,
        "notes": f"Installed from Gameyfin to {destination}",
    }


def uninstall_gameyfin_game(game):
    install_dir = Path(str(game.get("install_dir") or "")).expanduser()
    path = Path(str(game.get("path") or "")).expanduser()
    if not str(game.get("install_dir") or "").strip():
        raise GameyfinError("Refusing to uninstall a Gameyfin game without an install directory.")
    if install_dir.is_symlink() or path.is_symlink():
        raise GameyfinError("Refusing to uninstall through a symlink.")
    root = install_dir.resolve(strict=False)
    removed = []
    for candidate in (install_dir, path):
        if candidate.is_symlink():
            raise GameyfinError("Refusing to uninstall through a symlink.")
        candidate = candidate.resolve(strict=False)
        if candidate != root and root not in candidate.parents:
            raise GameyfinError(f"Refusing to remove a path outside the Gameyfin install directory: {candidate}")
        if not candidate.exists():
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        removed.append(str(candidate))
    game["store_installed"] = False
    game["path"] = str(install_dir) if install_dir.as_posix() else game.get("path", "")
    game["launch"] = ""
    game["notes"] = "Owned on Gameyfin. Install to download locally."
    return {"removed": removed, "game": game}


def test_gameyfin_connection(settings, client=None):
    conf = gameyfin_settings(settings)
    client = client or GameyfinClient(conf["url"], conf["username"], conf["password"])
    games = client.list_games()
    providers = client.list_providers()
    return {"ok": True, "games": len(games), "providers": providers}
