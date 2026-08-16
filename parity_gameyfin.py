"""Gameyfin self-hosted library client for OpenBox."""

from __future__ import annotations

import json
import base64
import binascii
import hashlib
import hmac
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from backend_io import fsync_directory, read_limited

DEFAULT_PROVIDER = "org.gameyfin.plugins.download.direct.DirectDownloadPlugin$DirectDownloadProvider"
GAMEYFIN_ID_RE = re.compile(r"[0-9]{1,20}\Z")
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024


class GameyfinError(ValueError):
    pass


def _slug(value):
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "game").casefold()).strip("-")
    return text or "game"


def validate_gameyfin_id(value):
    text = str(value or "").strip()
    if not GAMEYFIN_ID_RE.fullmatch(text):
        raise GameyfinError("Gameyfin IDs must be 1 to 20 decimal digits.")
    return str(int(text))


def _origin(url):
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise GameyfinError("Gameyfin URL must use HTTP(S) with a host.")
    if parsed.username or parsed.password:
        raise GameyfinError("Gameyfin URLs may not contain embedded credentials.")
    try:
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as error:
        raise GameyfinError("Gameyfin URL has an invalid port.") from error
    return parsed.scheme.casefold(), parsed.hostname.casefold(), port


def _same_origin(base_url, candidate_url):
    try:
        return _origin(base_url) == _origin(candidate_url)
    except GameyfinError:
        return False


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _same_origin(self.base_url, newurl):
            raise GameyfinError("Gameyfin redirect leaves the configured origin.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _expected_sha256(headers):
    for name in ("X-Checksum-SHA256", "X-SHA256", "Content-SHA256"):
        value = str(headers.get(name, "")).strip().casefold()
        if value:
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise GameyfinError(f"Gameyfin returned an invalid {name} checksum.")
            return value
    digest_header = str(headers.get("Digest", "")).strip()
    for item in digest_header.split(","):
        name, separator, value = item.partition("=")
        if separator and name.strip().casefold() == "sha-256":
            try:
                decoded = base64.b64decode(value.strip(), validate=True)
            except (binascii.Error, ValueError, TypeError) as error:
                raise GameyfinError("Gameyfin returned an invalid Digest checksum.") from error
            if len(decoded) != hashlib.sha256().digest_size:
                raise GameyfinError("Gameyfin returned an invalid Digest checksum.")
            return decoded.hex()
    return ""


def _atomic_download(response, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    headers = getattr(response, "headers", {}) or {}
    try:
        declared = int(headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as error:
        raise GameyfinError("Gameyfin returned an invalid Content-Length.") from error
    if declared < 0 or declared > MAX_DOWNLOAD_BYTES:
        raise GameyfinError("The Gameyfin download is too large.")
    expected = _expected_sha256(headers)
    digest = hashlib.sha256()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise GameyfinError("The Gameyfin download is too large.")
                digest.update(chunk)
                output.write(chunk)
            if declared and declared != total:
                raise GameyfinError("Gameyfin download length did not match its Content-Length.")
            output.flush()
            os.fsync(output.fileno())
        if expected and not hmac.compare_digest(digest.hexdigest(), expected):
            raise GameyfinError("Gameyfin download checksum verification failed.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_base_url(url):
    value = str(url or "").strip().rstrip("/")
    if not value:
        raise GameyfinError("Gameyfin URL is required.")
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = "https://" + value
    scheme, host, _port = _origin(value)
    if scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"} and os.environ.get("OPENBOX_ALLOW_HTTP_GAMEYFIN") != "1":
        raise GameyfinError("Gameyfin must use HTTPS unless HTTP is explicitly allowed for this deployment.")
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
        self.opener = opener or urllib.request.build_opener(
            _SameOriginRedirectHandler(self.base_url),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self._logged_in = False

    def request(self, method, path, data=None, headers=None, raw=False):
        path = str(path)
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path):
            if not _same_origin(self.base_url, path):
                raise GameyfinError("Gameyfin request leaves the configured origin.")
            url = path
        elif path.startswith("/"):
            url = f"{self.base_url}{path}"
        else:
            raise GameyfinError("Gameyfin request paths must be absolute paths.")
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
        final_url = response.geturl() if callable(getattr(response, "geturl", None)) else url
        if not _same_origin(self.base_url, final_url):
            response.close()
            raise GameyfinError("Gameyfin response came from an unexpected origin.")
        if raw:
            # The caller owns the response: it must consume it inside a
            # ``with response:`` block before the object goes out of scope.
            return response
        with response:
            content_type = response.headers.get("Content-Type", "")
            payload = read_limited(response, 16 * 1024 * 1024)
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
        response = self.request(
            "POST",
            "/login",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html,application/json"},
            raw=True,
        )
        # Only the session cookie matters; the body is never read.
        response.close()
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
        game_id = validate_gameyfin_id(game_id)
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
        final_url = response.geturl() if callable(getattr(response, "geturl", None)) else url
        if not _same_origin(self.base_url, final_url):
            response.close()
            raise GameyfinError("Gameyfin download came from an unexpected origin.")
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.I)
        filename = urllib.parse.unquote(match.group(1)) if match else f"gameyfin-{game_id}.bin"
        filename = Path(filename).name or f"gameyfin-{game_id}.bin"
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / filename
        with response:
            _atomic_download(response, target)
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


def _reject_symlink_components(path):
    cursor = Path(path)
    while True:
        try:
            if cursor.is_symlink():
                raise GameyfinError(f"Gameyfin paths may not contain symlinks: {cursor}")
        except OSError as error:
            raise GameyfinError(f"Could not inspect Gameyfin path: {cursor}") from error
        if cursor.parent == cursor:
            return
        cursor = cursor.parent


def _canonical_install_root(install_root):
    raw = Path(install_root).expanduser()
    if not raw.is_absolute():
        raise GameyfinError("Gameyfin install directory must be absolute.")
    _reject_symlink_components(raw)
    root = raw.resolve(strict=False)
    if root == Path("/"):
        raise GameyfinError("Refusing to use the filesystem root for Gameyfin installs.")
    try:
        info = root.lstat()
    except FileNotFoundError:
        return root
    except OSError as error:
        raise GameyfinError(f"Could not inspect Gameyfin install root: {root}") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or (info.st_mode & 0o022):
        raise GameyfinError("Gameyfin install root must be an owner-controlled, non-group-writable directory.")
    return root


def _contained_install_path(root, path, *, allow_root=False):
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise GameyfinError("Gameyfin install paths must be absolute.")
    _reject_symlink_components(raw)
    candidate = raw.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise GameyfinError(f"Gameyfin path escapes the configured install root: {candidate}") from error
    if not allow_root and candidate == root:
        raise GameyfinError("Refusing to use the configured install root as a game directory.")
    return candidate


def game_from_gameyfin(record, install_root, provider=""):
    if not isinstance(record, dict):
        raise GameyfinError("Gameyfin returned an invalid game record.")
    game_id = validate_gameyfin_id(record.get("id"))
    root = _canonical_install_root(install_root)
    title = str(record.get("title") or f"Gameyfin {game_id}")
    platforms = record.get("platforms") or []
    platform = platform_from_gameyfin(platforms)
    folder = _contained_install_path(root, root / f"{_slug(title)[:120].strip('-') or 'game'}-{game_id}")
    installed = folder.exists() and any(folder.iterdir()) if folder.is_dir() else folder.is_file()
    path = str(folder)
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


def _resolve_provider(client, conf, providers):
    provider = conf["provider"]
    if provider not in {item.get("key") for item in providers if isinstance(item, dict)}:
        provider = providers[0].get("key", DEFAULT_PROVIDER) if providers else DEFAULT_PROVIDER
    return provider


def catalog_gameyfin(settings, client=None):
    conf = gameyfin_settings(settings)
    if not conf["url"]:
        raise GameyfinError("Configure Gameyfin URL in Settings or Storefronts first.")
    install_root = _canonical_install_root(conf["install_dir"] or Path.home() / "Games" / "Gameyfin")
    client = client or GameyfinClient(conf["url"], conf["username"], conf["password"])
    providers = client.list_providers()
    provider = _resolve_provider(client, conf, providers)
    catalog = []
    for record in client.list_games():
        if not isinstance(record, dict) or record.get("id") is None:
            continue
        try:
            entry = game_from_gameyfin(record, install_root, provider)
        except GameyfinError:
            continue
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
    game_id = validate_gameyfin_id(game_id)
    conf = gameyfin_settings(settings)
    if not conf["url"]:
        raise GameyfinError("Configure Gameyfin URL first.")
    install_root = _canonical_install_root(conf["install_dir"] or Path.home() / "Games" / "Gameyfin")
    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(install_root, 0o700)
    except OSError as error:
        raise GameyfinError(f"Could not secure Gameyfin install root: {install_root}") from error
    client = client or GameyfinClient(conf["url"], conf["username"], conf["password"])
    records = {}
    for item in client.list_games():
        if not isinstance(item, dict):
            continue
        try:
            records[validate_gameyfin_id(item.get("id"))] = item
        except GameyfinError:
            continue
    record = records.get(game_id)
    if not record:
        raise GameyfinError(f"Gameyfin game {game_id} was not found.")
    providers = client.list_providers()
    provider = _resolve_provider(client, conf, providers)
    entry = game_from_gameyfin(record, install_root, provider)
    destination = _contained_install_path(install_root, entry["install_dir"])
    previous = _contained_install_path(install_root, destination.with_name(f".{destination.name}.openbox-previous"))
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.openbox-", dir=install_root))
    _contained_install_path(install_root, staging)
    if staging.exists():
        shutil.rmtree(staging) if staging.is_dir() else staging.unlink()
    if previous.exists():
        shutil.rmtree(previous) if previous.is_dir() else previous.unlink()
    staging.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = client.download_game(game_id, provider, staging)
        downloaded = Path(downloaded).expanduser()
        downloaded = _contained_install_path(staging.resolve(strict=False), downloaded)
        if not downloaded.is_file():
            raise GameyfinError("Gameyfin download did not produce a regular file.")
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


def uninstall_gameyfin_game(game, install_root):
    root = _canonical_install_root(install_root)
    install_dir_text = str(game.get("install_dir") or "").strip()
    path_text = str(game.get("path") or "").strip()
    install_dir = _contained_install_path(root, Path(install_dir_text)) if install_dir_text else None
    path = _contained_install_path(root, Path(path_text)) if path_text else None
    if not str(game.get("install_dir") or "").strip():
        raise GameyfinError("Refusing to uninstall a Gameyfin game without an install directory.")
    removed = []
    seen = set()
    for candidate in (install_dir, path):
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
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
