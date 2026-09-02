"""Optional ScreenScraper metadata/media provider (1.8.0, ADR 0022).

ScreenScraper (https://www.screenscraper.fr) is the emulation community's
per-ROM-hash scraping service. This module mirrors parity_igdb.py: pure
stdlib HTTP via urllib, credentials from ~/.env (never settings JSON),
polite rate limiting, and a local disk cache to respect quotas.

All network calls are user-triggered (search, hash-match, apply); nothing
here scrapes a whole library in the background.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend_io import read_limited
from env_config import ensure_env_loaded

SS_ENDPOINT = "https://www.screenscraper.fr/api2"
SS_CACHE_TTL = 30 * 24 * 3600
_MIN_REQUEST_INTERVAL = 1.0
_HASH_SIZE_LIMIT = 512 * 1024 * 1024
_MAX_RETRIES = 3

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]

# OpenBox platform names -> ScreenScraper system ids (systems list subset;
# ids match ScreenScraper's public systems.php values). Platforms left out
# fall back to name-only search and system-less hash matching, which the
# service supports.
PLATFORM_SYSTEM_IDS = {
    "arcade": 11,
    "dreamcast": 23,
    "game boy": 9,
    "game boy advance": 12,
    "game boy color": 10,
    "gamecube": 13,
    "nes": 3,
    "nintendo 64": 14,
    "playstation": 2,
    "playstation 2": 15,
    "playstation 3": 65,
    "playstation vita": 68,
    "psp": 61,
    "sega saturn": 17,
    "scummvm": 123,
    "snes": 4,
    "switch": 225,
    "nintendo switch": 225,
    "wii": 16,
    "wii u": 18,
    "xbox": 29,
    "xbox 360": 32,
}

# LaunchBox-style region names -> ScreenScraper region codes.
REGION_CODES = {
    "world": "wor", "north america": "us", "europe": "eu", "japan": "jp",
    "australia": "au", "brazil": "br", "canada": "ca", "china": "cn",
    "france": "fr", "germany": "de", "italy": "it", "korea": "ko",
    "netherlands": "nl", "spain": "es", "sweden": "se", "united kingdom": "uk",
}

# ScreenScraper media types -> OpenBox media kinds.
MEDIA_TYPE_MAP = {
    "box2D": "cover", "boxback": "box_back", "boxspine": "box_spine",
    "box3D": "box_3d", "fanart": "fanart", "banner": "banner",
    "cart": "cart_front", "cart2": "cart_back", "disc": "disc",
    "image": "title_screen", "logo": "clear_logo", "flyer": "advertisement",
    "video": "video_snap", "manual": "manual",
}

_ROM_TYPE_BY_SUFFIX = {
    ".zip": "zip", ".7z": "zip", ".rar": "zip",
    ".iso": "iso", ".chd": "chd", ".gcm": "iso", ".wbfs": "wbfs", ".rvz": "rvz",
    ".xci": "xci", ".nsp": "nsp", ".wad": "wad", ".cia": "cia", ".3ds": "3ds",
}


def credentials():
    """Return (user, password, dev_id, dev_password) from ~/.env."""
    ensure_env_loaded()
    user = os.environ.get("SCREENSCRAPER_USER", "").strip()
    password = os.environ.get("SCREENSCRAPER_PASSWORD", "").strip()
    if not user or not password:
        raise ValueError("Set SCREENSCRAPER_USER and SCREENSCRAPER_PASSWORD in ~/.env to use ScreenScraper.")
    dev_id = os.environ.get("SCREENSCRAPER_DEV_ID", "").strip()
    dev_password = os.environ.get("SCREENSCRAPER_DEV_PASSWORD", "").strip()
    return user, password, dev_id, dev_password


def system_id_for_platform(platform):
    """Map an OpenBox platform name to a ScreenScraper system id (or None)."""
    key = str(platform or "").strip().casefold()
    return PLATFORM_SYSTEM_IDS.get(key)


def region_codes(region_priority):
    """Translate the settings.region_priority names to SS region codes."""
    codes = []
    for name in region_priority or []:
        code = REGION_CODES.get(str(name).strip().casefold())
        if code and code not in codes:
            codes.append(code)
    return codes or ["wor", "us", "eu", "jp"]


def _throttle():
    with _REQUEST_LOCK:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[0] = time.monotonic()


def ss_request(endpoint, params, *, timeout=30):
    """GET an api2 endpoint as JSON with credentials, throttling, and retries."""
    user, password, dev_id, dev_password = credentials()
    query = dict(params)
    query["output"] = "json"
    query["user"] = user
    query["sspassword"] = password
    if dev_id and dev_password:
        query["devid"] = dev_id
        query["devpassword"] = dev_password
    url = f"{SS_ENDPOINT}/{endpoint}?{urlencode(query)}"
    last_error = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            request = Request(url, headers={"User-Agent": "OpenBox/1.8.0"})
            with urlopen(request, timeout=timeout) as response:
                return json.loads(read_limited(response, 8 * 1024 * 1024).decode())
        except Exception as error:  # urllib raises HTTPError/URLError subclasses
            last_error = error
            status = getattr(error, "code", 0)
            if status and status not in (429, 500, 502, 503, 504):
                break
            time.sleep(1.5 * (attempt + 1))
    raise ValueError(f"ScreenScraper request failed: {last_error}")


# ── Disk cache ─────────────────────────────────────────────────────────────

def _cache_key(params):
    canonical = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
    return hashlib.sha1(canonical.encode()).hexdigest()


def cache_get(cache_dir, params):
    path = Path(cache_dir) / "screenscraper" / f"{_cache_key(params)}.json"
    try:
        if path.is_file() and time.time() - path.stat().st_mtime < SS_CACHE_TTL:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


def cache_put(cache_dir, params, payload):
    directory = Path(cache_dir) / "screenscraper"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{_cache_key(params)}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def cache_size(cache_dir):
    directory = Path(cache_dir) / "screenscraper"
    if not directory.is_dir():
        return 0
    return sum(1 for item in directory.iterdir() if item.is_file())


# ── API operations ─────────────────────────────────────────────────────────

def user_info(cache_dir=None):
    """Credential + quota check via userInfos.php."""
    params = {"ssid": "userinfos"}
    if cache_dir is not None:
        cached = cache_get(cache_dir, params)
        if cached is not None:
            return cached
    payload = ss_request("userInfos.php", {"ssid": "userinfos"})
    if cache_dir is not None:
        cache_put(cache_dir, params, payload)
    return payload


def search_games(name, system_id=None, limit=12):
    """Text search via jeuRecherche.php -> [{id, name, system_id, system_name, year}]."""
    name = str(name).strip()
    if not name:
        return []
    try:
        limit = min(50, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 12
    payload = ss_request("jeuRecherche.php", {"recherche": name, "systemeid": system_id or "", "st": 0})
    rows = (((payload.get("response") or {}).get("jeux")) or [])
    results = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        results.append({
            "id": row.get("id"),
            "name": row.get("name", ""),
            "system_id": row.get("systemid"),
            "system_name": row.get("systemname", ""),
            "year": str(row.get("year") or ""),
        })
    return results


def hash_rom(path):
    """Stream md5/sha1/crc of *path* (capped) for per-ROM-hash matching."""
    rom_path = Path(path)
    if not rom_path.is_file():
        raise ValueError(f"ROM file not found: {rom_path.name}")
    if rom_path.stat().st_size > _HASH_SIZE_LIMIT:
        raise ValueError("ROM file is too large to hash for scraping.")
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    crc = 0
    with rom_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            crc = zlib.crc32(chunk, crc)
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "crc": f"{crc:08x}", "size": rom_path.stat().st_size}


def rom_type_for(path):
    suffix = Path(path).suffix.casefold()
    return _ROM_TYPE_BY_SUFFIX.get(suffix, "rom")


def game_info(game_id=None, *, rom_path=None, system_id=None, cache_dir=None):
    """Fetch one game's metadata via jeuInfos.php (by id or by ROM hash).

    Returns a normalized metadata dict with a `media` list of
    {kind, url, region, order} entries. Raises ValueError when the game
    is not found.
    """
    params = {}
    if game_id is not None:
        params["gameid"] = int(game_id)
    elif rom_path:
        hashes = hash_rom(rom_path)
        rom_type = rom_type_for(rom_path)
        params.update({
            "romtype": rom_type,
            "rommd5": hashes["md5"],
            "romsha1": hashes["sha1"],
            "romcrc": hashes["crc"],
        })
    else:
        raise ValueError("game_info needs a game id or a ROM path.")
    if system_id:
        params["systemeid"] = int(system_id)
    if cache_dir is not None:
        cached = cache_get(cache_dir, params)
        if cached is not None:
            return _normalize_jeu(cached)
    payload = ss_request("jeuInfos.php", params)
    jeu = ((payload.get("response") or {}).get("jeu")) or {}
    if not jeu:
        raise ValueError("Game not found on ScreenScraper.")
    if cache_dir is not None:
        cache_put(cache_dir, params, payload)
    return _normalize_jeu(payload)


def _text(node):
    if isinstance(node, dict):
        return str(node.get("text", "") or "").strip()
    return str(node or "").strip()


def _normalize_jeu(payload):
    jeu = (payload.get("response") or {}).get("jeu") or {}
    media_entries = []
    for media in jeu.get("medias") or []:
        if not isinstance(media, dict):
            continue
        url = str(media.get("url", "") or "").strip()
        if not url:
            continue
        media_entries.append({
            "type": str(media.get("type", "") or ""),
            "kind": MEDIA_TYPE_MAP.get(str(media.get("type", "")), ""),
            "url": url,
            "region": str(media.get("region", "") or ""),
            "order": media.get("order") if isinstance(media.get("order"), int) else 0,
        })
    title_screen = [entry for entry in media_entries if entry["type"] == "ss"]
    media_entries.extend({**entry, "kind": "screenshots"} for entry in title_screen)
    scraper_id = jeu.get("id")
    return {
        "id": scraper_id if isinstance(scraper_id, int) else None,
        "name": _text(jeu.get("nom")),
        "description": _text(jeu.get("synopsis")),
        "year": _text(jeu.get("year")),
        "developer": _text(jeu.get("developpeur")),
        "publisher": _text(jeu.get("editeur")),
        "genre": _text(jeu.get("genre")),
        "players": _text(jeu.get("players")),
        "rating": _rating(jeu.get("note")),
        "region": _text(jeu.get("region")),
        "media": media_entries,
    }


def _rating(node):
    value = _text(node)
    if not value:
        return None
    try:
        return round(float(value) / 20, 2)  # SS grades /20 -> OpenBox /5
    except ValueError:
        return None


def choose_media(metadata, media_kinds, region_priority=None, limit=20):
    """Pick the best URL per requested kind, honoring region priority.

    Returns {openbox_kind: url}. 'screenshots' yields up to *limit* urls
    under the key 'screenshots' as a list.
    """
    codes = region_codes(region_priority)
    order = {code: index for index, code in enumerate(codes)}
    chosen = {}
    screenshots = []
    for entry in metadata.get("media", []):
        kind = entry.get("kind")
        if kind not in media_kinds and kind != "screenshots":
            continue
        if kind == "screenshots":
            screenshots.append((order.get(entry["region"], 99), entry["order"], entry["url"]))
            continue
        current = chosen.get(kind)
        rank = (order.get(entry["region"], 99), entry["order"])
        if current is None or rank < current[0]:
            chosen[kind] = (rank, entry["url"])
    result = {kind: url for kind, (_rank, url) in chosen.items()}
    if "screenshots" in media_kinds:
        screenshots.sort(key=lambda item: (item[0], item[1]))
        result["screenshots"] = [url for _region, _order, url in screenshots[:limit]]
    return result


def apply_to_game(game, metadata, fields=("name", "description", "year", "genre", "developer", "publisher")):
    """Apply selected metadata fields onto a game dict (mirrors IGDB apply)."""
    if not isinstance(game, dict) or not isinstance(metadata, dict):
        raise ValueError("Invalid ScreenScraper metadata payload.")
    for field in fields:
        value = metadata.get(field)
        if value:
            game[field] = value
    if metadata.get("rating") is not None:
        game["rating"] = metadata["rating"]
    if metadata.get("id") is not None:
        game["screenscraper_id"] = metadata["id"]
    return game


def is_configured():
    try:
        credentials()
        return True
    except ValueError:
        return False


def clean_media_url(url):
    """Reject non-https or injection-looking URLs before download."""
    candidate = str(url or "").strip()
    return candidate if re.match(r"^https://[A-Za-z0-9.\-]+/", candidate) else ""
