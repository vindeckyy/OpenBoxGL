"""Optional SteamGridDB artwork provider (S4; ADR-0022 provider precedent).

SteamGridDB (https://www.steamgriddb.com) supplies community artwork:
vertical grids (box fronts), horizontal grids (banners), heroes
(backgrounds), logos, and icons. This module mirrors
``parity_screenscraper.py``: pure-stdlib urllib transport, credentials
from ~/.env (``STEAMGRIDDB_API_KEY``, never settings JSON), an https-only
endpoint, a local disk cache, and exponential backoff on HTTP 429/5xx.

A rejected key (401/403) disables the provider for the rest of the
process so the failure surfaces once instead of once per request. All
network calls are user-triggered (search, apply, bulk match); nothing
here scans a whole library in the background.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from backend_io import read_limited
from env_config import ensure_env_loaded, env_value
from updates import VERSION

SGDB_ENDPOINT = "https://www.steamgriddb.com/api/v2"
SGDB_CACHE_TTL = 30 * 24 * 3600
PROVIDER = "steamgrid"

# Canonical provider ordering for merged metadata search results; the
# metadata dialog appends provider-tagged rows in this order.
PROVIDER_ORDER = ("launchbox", "igdb", "screenscraper", PROVIDER)

_MIN_REQUEST_INTERVAL = 1.0
_MAX_RETRIES = 4
_BACKOFF_BASE = 1.5
_RETRY_AFTER_CAP = 60.0

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]
_SESSION_DISABLED = {"value": False}

# SteamGridDB grid dimensions: tall grids are box fronts, wide ones banners.
_GRID_COVER_DIMENSIONS = "600x900,342x482,660x930"
_GRID_BANNER_DIMENSIONS = "460x215,920x430"

# (OpenBox media kind, SGDB asset endpoint prefix, extra query params).
ASSET_ENDPOINTS = (
    ("cover", "grids", {"dimensions": _GRID_COVER_DIMENSIONS}),
    ("banner", "grids", {"dimensions": _GRID_BANNER_DIMENSIONS}),
    ("background", "heroes", {}),
    ("clear_logo", "logos", {}),
    ("icon", "icons", {}),
)


def credentials():
    """Return the SteamGridDB API key from ~/.env or the environment."""
    ensure_env_loaded()
    key = env_value("STEAMGRIDDB_API_KEY", "OPENBOX_STEAMGRIDDB_API_KEY")
    if not key:
        raise ValueError("Set STEAMGRIDDB_API_KEY in ~/.env to use SteamGridDB.")
    return key


def is_configured():
    try:
        credentials()
        return True
    except ValueError:
        return False


def enabled(settings):
    """Provider toggle: ``steamgrid_enabled`` defaults on."""
    return bool((settings or {}).get("steamgrid_enabled", True))


def session_disabled():
    """True after a 401/403 — the key was rejected; disabled until restart."""
    return _SESSION_DISABLED["value"]


def provider_available(settings):
    return enabled(settings) and is_configured() and not session_disabled()


def provider_status(settings, cache_dir=None):
    status = {
        "provider": PROVIDER,
        "enabled": enabled(settings),
        "configured": is_configured(),
        "disabled": session_disabled(),
    }
    if cache_dir is not None:
        status["cache_entries"] = cache_size(cache_dir)
    return status


def _throttle():
    with _REQUEST_LOCK:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[0] = time.monotonic()


def _retry_delay(error, attempt):
    headers = getattr(error, "headers", None)
    retry_after = ""
    if headers is not None:
        try:
            retry_after = headers.get("Retry-After", "")
        except AttributeError:
            retry_after = ""
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        return min(seconds, _RETRY_AFTER_CAP)
    return _BACKOFF_BASE * (2 ** attempt)


def sgdb_request(path, params=None, *, timeout=30):
    """GET one api/v2 endpoint as JSON: Bearer auth, throttle, 429 backoff.

    The endpoint is pinned to https; 401/403 marks the provider disabled
    for the session and every other non-retryable status fails fast.
    """
    api_key = credentials()
    url = f"{SGDB_ENDPOINT}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urlencode(params)}"
    if not url.startswith("https://"):
        raise ValueError("SteamGridDB requests must stay on https.")
    if _SESSION_DISABLED["value"]:
        raise ValueError("SteamGridDB is disabled for this session (the API key was rejected).")
    last_error = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            request = Request(url, headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": f"OpenBox/{VERSION}",
            })
            with urlopen(request, timeout=timeout) as response:
                return json.loads(read_limited(response, 8 * 1024 * 1024).decode())
        except Exception as error:  # urllib raises HTTPError/URLError subclasses
            last_error = error
            status = getattr(error, "code", 0)
            if status in (401, 403):
                _SESSION_DISABLED["value"] = True
                raise ValueError(
                    "SteamGridDB rejected the API key; the provider is disabled until restart."
                ) from error
            if status == 429 or status in (500, 502, 503, 504):
                if attempt + 1 < _MAX_RETRIES:
                    time.sleep(_retry_delay(error, attempt))
                continue
            break
    raise ValueError(f"SteamGridDB request failed: {last_error}")


# ── Disk cache ─────────────────────────────────────────────────────────────

def _cache_key(params):
    canonical = urlencode(sorted((str(key), str(value)) for key, value in params.items()))
    return hashlib.sha1(canonical.encode()).hexdigest()


def cache_get(cache_dir, params):
    path = Path(cache_dir) / "steamgrid" / f"{_cache_key(params)}.json"
    try:
        if path.is_file() and time.time() - path.stat().st_mtime < SGDB_CACHE_TTL:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


def cache_put(cache_dir, params, payload):
    directory = Path(cache_dir) / "steamgrid"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{_cache_key(params)}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def cache_size(cache_dir):
    directory = Path(cache_dir) / "steamgrid"
    if not directory.is_dir():
        return 0
    return sum(1 for item in directory.iterdir() if item.is_file())


# ── API operations ─────────────────────────────────────────────────────────

def search_games(name, limit=12, cache_dir=None):
    """Autocomplete search -> [{id, name, year, types, verified, provider}]."""
    name = str(name).strip()
    if not name:
        return []
    try:
        limit = min(50, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 12
    params = {"sgdb_search": name, "limit": limit}
    if cache_dir is not None:
        cached = cache_get(cache_dir, params)
        if cached is not None:
            return cached
    payload = sgdb_request(f"search/autocomplete/{quote(name, safe='')}")
    results = []
    for row in (payload.get("data") or [])[:limit]:
        if not isinstance(row, dict):
            continue
        results.append({
            "id": row.get("id"),
            "name": str(row.get("name", "") or ""),
            "year": _year_from_timestamp(row.get("release_date")),
            "types": [str(item) for item in row.get("types") or []],
            "verified": bool(row.get("verified")),
            "provider": PROVIDER,
        })
    if cache_dir is not None:
        cache_put(cache_dir, params, results)
    return results


def game_assets(game_id, cache_dir=None):
    """Return the media list for one SGDB game id, best-scored first.

    Entries are ``{kind, url, thumb, score, style, width, height}``; NSFW-,
    epilepsy-, and humor-flagged assets are filtered out so bulk fills can
    never surprise.
    """
    gid = _game_id(game_id)
    params = {"sgdb_assets": gid}
    if cache_dir is not None:
        cached = cache_get(cache_dir, params)
        if cached is not None:
            return cached
    media = []
    for kind, prefix, extra in ASSET_ENDPOINTS:
        payload = sgdb_request(f"{prefix}/game/{gid}", dict(extra) or None)
        for row in payload.get("data") or []:
            entry = _asset_entry(kind, row)
            if entry is not None:
                media.append(entry)
    media.sort(key=lambda entry: -entry["score"])
    if cache_dir is not None:
        cache_put(cache_dir, params, media)
    return media


def game_info(game_id, cache_dir=None):
    """Normalized metadata + media for one SGDB game id."""
    gid = _game_id(game_id)
    params = {"sgdb_game": gid}
    if cache_dir is not None:
        cached = cache_get(cache_dir, params)
        if cached is not None:
            return cached
    data = (sgdb_request(f"games/id/{gid}").get("data")) or {}
    if not data.get("id") and not data.get("name"):
        raise ValueError("Game not found on SteamGridDB.")
    metadata = {
        "id": data.get("id") if isinstance(data.get("id"), int) else gid,
        "name": str(data.get("name", "") or ""),
        "year": _year_from_timestamp(data.get("release_date")),
        "types": [str(item) for item in data.get("types") or []],
        "verified": bool(data.get("verified")),
        "media": game_assets(gid, cache_dir=cache_dir),
    }
    if cache_dir is not None:
        cache_put(cache_dir, params, metadata)
    return metadata


def _game_id(game_id):
    try:
        return int(game_id)
    except (TypeError, ValueError):
        raise ValueError("A numeric SteamGridDB game id is required.") from None


def _asset_entry(kind, row):
    if not isinstance(row, dict):
        return None
    if row.get("nsfw") or row.get("epilepsy") or row.get("humor"):
        return None
    url = str(row.get("url") or "").strip()
    if not url:
        return None
    score = row.get("score")
    width = row.get("width")
    height = row.get("height")
    return {
        "kind": kind,
        "url": url,
        "thumb": str(row.get("thumb") or ""),
        "score": score if isinstance(score, (int, float)) else 0,
        "style": str(row.get("style") or ""),
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
    }


def _year_from_timestamp(value):
    try:
        return str(time.gmtime(int(value)).tm_year)
    except (OverflowError, TypeError, ValueError, OSError):
        return ""


def choose_media(metadata, media_kinds):
    """Pick the top-scored https URL per requested kind -> {kind: url}.

    *metadata* may be a normalized payload (``{"media": [...]}``) or a bare
    media list; entries arrive score-sorted so the first valid URL per
    kind wins.
    """
    entries = metadata.get("media", []) if isinstance(metadata, dict) else metadata
    wanted = set(media_kinds or [])
    ordered = sorted(
        (entry for entry in entries or [] if isinstance(entry, dict)),
        key=lambda entry: -(entry.get("score") if isinstance(entry.get("score"), (int, float)) else 0),
    )
    chosen = {}
    for entry in ordered:
        kind = entry.get("kind")
        if kind not in wanted or kind in chosen:
            continue
        url = clean_media_url(entry.get("url"))
        if url:
            chosen[kind] = url
    return chosen


def merge_results(existing, additions, provider=PROVIDER):
    """Merge provider-tagged additions into results in canonical order.

    Rows carry a ``provider`` field; ordering follows PROVIDER_ORDER
    (launchbox, igdb, screenscraper, steamgrid) and is stable within a
    provider. Unknown providers sort last.
    """
    rank = {name: index for index, name in enumerate(PROVIDER_ORDER)}
    merged = [item for item in existing or [] if isinstance(item, dict)]
    merged += [
        {**item, "provider": item.get("provider") or provider}
        for item in additions or []
        if isinstance(item, dict)
    ]
    merged.sort(key=lambda item: rank.get(item.get("provider"), len(PROVIDER_ORDER)))
    return merged


def apply_to_game(game, metadata, fields=("name", "year")):
    """Apply selected metadata fields onto a game dict (mirrors SS apply)."""
    if not isinstance(game, dict) or not isinstance(metadata, dict):
        raise ValueError("Invalid SteamGridDB metadata payload.")
    for field in fields:
        value = metadata.get(field)
        if value:
            game[field] = value
    if metadata.get("id") is not None:
        game["steamgrid_id"] = metadata["id"]
    return game


def clean_media_url(url):
    """Reject non-https or injection-looking URLs before download."""
    candidate = str(url or "").strip()
    return candidate if re.match(r"^https://[A-Za-z0-9.\-]+/", candidate) else ""
