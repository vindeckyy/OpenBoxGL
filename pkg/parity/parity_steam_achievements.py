"""Steam achievements and playtime integration (P11.1).

The Steam Web API is HTTPS-only and key-based. Credentials live in
``<data>/steam.json`` (0600) with ``STEAM_API_KEY``/``STEAM_ID`` as an
environment fallback; the key never enters ``settings.json`` and is never
returned by the credential helpers. Per-app achievement payloads are cached
under ``<data>/cache/steam/<app_id>.json`` (0600, 24 h TTL) so opening a
details pane does not hit the API on every render.

Everything that touches the network is injectable (``opener``, ``clock``,
``sleep``) so tests never open a socket; requests are throttled to one per
second and retried twice (2 s, 8 s) on HTTP 429/5xx only. A 4xx is final.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend_io import atomic_write_text, read_limited
from pkg.platform_compat import parse_timestamp, resolved_data_dir

STEAM_API_ROOT = "https://api.steampowered.com"
STEAM_CACHE_TTL = 24 * 3600
MAX_ACHIEVEMENT_ITEMS = 500
MAX_OWNED_GAMES = 10_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 10.0
MIN_REQUEST_INTERVAL = 1.0
_RETRY_DELAYS = (2.0, 8.0)
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]


# ── Credentials ────────────────────────────────────────────────────────────

def data_directory(data_dir=None) -> Path:
    """Resolve the OpenBox data directory (``OPENBOX_DATA_DIR`` aware)."""
    if data_dir is not None:
        return Path(data_dir)
    return Path(resolved_data_dir())


def _steam_file(data_dir=None) -> Path:
    return data_directory(data_dir) / "steam.json"


def load_steam_credentials(data_dir=None) -> dict:
    """Return ``{"api_key": str, "steam_id": str}`` from file, then env.

    A malformed or unreadable ``steam.json`` is treated as absent. File values
    win; an environment variable fills only a value the file does not have.
    """
    values = {"api_key": "", "steam_id": ""}
    path = _steam_file(data_dir)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, Mapping):
            values["api_key"] = str(payload.get("api_key") or "").strip()
            values["steam_id"] = str(payload.get("steam_id") or "").strip()
    values["api_key"] = values["api_key"] or os.environ.get("STEAM_API_KEY", "").strip()
    values["steam_id"] = values["steam_id"] or os.environ.get("STEAM_ID", "").strip()
    return values


def save_steam_credentials(data_dir=None, *, api_key=None, steam_id=None) -> dict:
    """Merge non-``None`` fields into ``<data>/steam.json`` (0600).

    Returns a redacted status object; the API key is never echoed back.
    """
    existing = load_steam_credentials(data_dir)
    values = {
        "api_key": existing["api_key"] if api_key is None else str(api_key).strip(),
        "steam_id": existing["steam_id"] if steam_id is None else str(steam_id).strip(),
    }
    path = _steam_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(values, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, payload, mode=0o600)
    return credential_status(values)


def credential_status(values=None, data_dir=None) -> dict:
    """Return ``{configured, steam_id, key_set}``; never includes the key."""
    if values is None:
        values = load_steam_credentials(data_dir)
    api_key = str(values.get("api_key") or "")
    steam_id = str(values.get("steam_id") or "")
    return {
        "configured": bool(api_key and steam_id),
        "steam_id": steam_id,
        "key_set": bool(api_key),
    }


def _api_key(api_key, data_dir) -> str:
    if api_key is not None:
        value = str(api_key).strip()
    else:
        value = load_steam_credentials(data_dir)["api_key"]
    if not value:
        raise ValueError("Configure a Steam API key in Settings first.")
    return value


def _steam_id(value) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or len(text) > 20:
        raise ValueError("A numeric Steam ID is required.")
    return text


def _app_id(value) -> int:
    try:
        app_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("A numeric Steam app id is required.") from None
    if app_id <= 0:
        raise ValueError("A numeric Steam app id is required.")
    return app_id


# ── HTTP ───────────────────────────────────────────────────────────────────

def _throttle(clock, sleep):
    with _REQUEST_LOCK:
        wait = MIN_REQUEST_INTERVAL - (clock() - _LAST_REQUEST[0])
        if wait > 0:
            sleep(wait)
        _LAST_REQUEST[0] = clock()


def _request_json(url, *, opener, clock, sleep, timeout=REQUEST_TIMEOUT) -> dict:
    """GET one HTTPS endpoint as a JSON object with throttle and retries."""
    if not str(url).startswith("https://"):
        raise ValueError("Steam requests must stay on https.")
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    last_error = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        _throttle(clock, sleep)
        try:
            request = Request(url, headers={"User-Agent": "OpenBox/steam-achievements"})
            with closing(opener(request, timeout=timeout)) as response:
                body = read_limited(response, MAX_RESPONSE_BYTES).decode("utf-8", "replace")
            payload = json.loads(body)
        except Exception as error:  # urllib raises HTTPError/URLError subclasses
            last_error = error
            status = getattr(error, "code", 0)
            if status in _RETRYABLE_STATUSES and attempt < len(_RETRY_DELAYS):
                sleep(_RETRY_DELAYS[attempt])
                continue
            break
        if not isinstance(payload, dict):
            raise ValueError("Steam returned an invalid response.")
        return payload
    raise ValueError(f"Steam request failed: {last_error}")


def _achievement_rows(payload) -> list:
    """Extract achievement rows from a normalized list or raw API payload."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for container in ("game", "playerstats"):
        candidate = payload.get(container)
        if isinstance(candidate, Mapping):
            if "achievements" in candidate:
                rows = candidate.get("achievements")
                return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
            nested = candidate.get("availableGameStats")
            if isinstance(nested, Mapping):
                rows = nested.get("achievements")
                return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    rows = payload.get("achievements")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _player_success(payload):
    if isinstance(payload, Mapping):
        stats = payload.get("playerstats")
        if isinstance(stats, Mapping) and "success" in stats:
            return bool(stats.get("success"))
        if "success" in payload:
            return bool(payload.get("success"))
    return True


def _normalize_schema_row(row: Mapping) -> dict:
    return {
        "apiname": str(row.get("name") or row.get("apiname") or "").strip(),
        "title": str(row.get("displayName") or row.get("title") or "").strip(),
        "description": str(row.get("description") or "").strip(),
        "hidden": bool(row.get("hidden", 0)),
        "icon": str(row.get("icon") or "").strip(),
        "icon_gray": str(row.get("icongray") or "").strip(),
    }


def _normalize_player_row(row: Mapping) -> dict:
    try:
        achieved = int(row.get("achieved", 0) or 0)
    except (TypeError, ValueError):
        achieved = 0
    try:
        unlock_time = int(row.get("unlocktime", 0) or 0)
    except (TypeError, ValueError):
        unlock_time = 0
    return {
        "apiname": str(row.get("apiname") or row.get("name") or "").strip(),
        "achieved": 1 if achieved == 1 else 0,
        "unlock_time": max(0, unlock_time),
    }


def fetch_schema(app_id, opener=urlopen, *, api_key=None, data_dir=None,
                 timeout=REQUEST_TIMEOUT, clock=None, sleep=None) -> list:
    """Fetch the achievement schema for an app as normalized rows."""
    app_id = _app_id(app_id)
    key = _api_key(api_key, data_dir)
    query = urlencode({"key": key, "appid": app_id})
    url = f"{STEAM_API_ROOT}/ISteamUserStats/GetSchemaForGame/v2/?{query}"
    payload = _request_json(url, opener=opener, clock=clock, sleep=sleep, timeout=timeout)
    return [_normalize_schema_row(row) for row in _achievement_rows(payload)]


def fetch_player(app_id, steam_id, opener=urlopen, *, api_key=None, data_dir=None,
                 timeout=REQUEST_TIMEOUT, clock=None, sleep=None) -> dict:
    """Fetch one player's achievement state for an app.

    Returns ``{"success": bool, "achievements": [normalized rows]}``; Steam
    reports ``success: false`` for private profiles and for apps that may not
    be played through the Web API.
    """
    app_id = _app_id(app_id)
    steam_id = _steam_id(steam_id)
    key = _api_key(api_key, data_dir)
    query = urlencode({
        "key": key,
        "steamid": steam_id,
        "appid": app_id,
        "l": "english",
    })
    url = f"{STEAM_API_ROOT}/ISteamUserStats/GetPlayerAchievements/v1/?{query}"
    payload = _request_json(url, opener=opener, clock=clock, sleep=sleep, timeout=timeout)
    rows = [_normalize_player_row(row) for row in _achievement_rows(payload)]
    return {"success": _player_success(payload), "achievements": rows}


def fetch_owned_games(steam_id, opener=urlopen, *, api_key=None, data_dir=None,
                      timeout=REQUEST_TIMEOUT, clock=None, sleep=None) -> list:
    """Fetch the owned-games list with playtime and last-played timestamps."""
    steam_id = _steam_id(steam_id)
    key = _api_key(api_key, data_dir)
    query = urlencode({
        "key": key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    })
    url = f"{STEAM_API_ROOT}/IPlayerService/GetOwnedGames/v1/?{query}"
    payload = _request_json(url, opener=opener, clock=clock, sleep=sleep, timeout=timeout)
    response = payload.get("response")
    games = response.get("games") if isinstance(response, Mapping) else None
    if not isinstance(games, list):
        return []
    owned = []
    for entry in games[:MAX_OWNED_GAMES]:
        if not isinstance(entry, Mapping):
            continue
        try:
            app_id = int(entry.get("appid", 0) or 0)
        except (TypeError, ValueError):
            continue
        if app_id <= 0:
            continue
        owned.append({
            "appid": app_id,
            "name": str(entry.get("name") or "").strip(),
            "playtime_forever": _nonnegative_int(entry.get("playtime_forever")),
            "rtime_last_played": _nonnegative_int(entry.get("rtime_last_played")),
        })
    return owned


# ── Merge ──────────────────────────────────────────────────────────────────

def merge_schema_player(schema, player):
    """Merge a schema and a player payload into one Steam achievement source.

    ``total`` counts schema achievements (capped at
    :data:`MAX_ACHIEVEMENT_ITEMS`). A schema with no achievements, or a player
    payload whose ``success`` is false, produces no source entry (``None``) so
    the UI never renders an empty Steam tab.
    """
    if not _player_success(player):
        return None
    player_rows = {}
    for row in _achievement_rows(player):
        normalized = _normalize_player_row(row)
        if normalized["apiname"] and normalized["apiname"] not in player_rows:
            player_rows[normalized["apiname"]] = normalized
    items = []
    for row in _achievement_rows(schema):
        normalized = _normalize_schema_row(row)
        apiname = normalized["apiname"]
        if not apiname or any(item["id"] == apiname for item in items):
            continue
        progress = player_rows.get(apiname, {})
        items.append({
            "id": apiname,
            "title": normalized["title"] or apiname,
            "description": normalized["description"],
            "points": 0,
            "hidden": normalized["hidden"],
            "icon_url": normalized["icon"],
            "icon_gray_url": normalized["icon_gray"],
            "earned": bool(progress.get("achieved")),
            "unlock_time": int(progress.get("unlock_time") or 0),
        })
        if len(items) >= MAX_ACHIEVEMENT_ITEMS:
            break
    total = len(items)
    if total == 0:
        return None
    earned = sum(1 for item in items if item["earned"])
    return {
        "source": "steam",
        "name": "Steam",
        "earned": earned,
        "total": total,
        "progress_pct": round(earned * 100.0 / total, 1),
        "items": items,
    }


# ── Disk cache ─────────────────────────────────────────────────────────────

def cache_path(data_dir, app_id) -> Path:
    return data_directory(data_dir) / "cache" / "steam" / f"{_app_id(app_id)}.json"


def load_cache(data_dir, app_id, *, now=None, ttl=STEAM_CACHE_TTL):
    """Return the cached payload when fresh, else ``None``.

    A malformed, non-object, or unreadable cache reads as empty rather than
    raising; a corrupt entry must never break the details pane.
    """
    path = cache_path(data_dir, app_id)
    try:
        if not path.is_file():
            return None
        current = time.time() if now is None else float(now)
        if current - path.stat().st_mtime > ttl:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_cache(data_dir, app_id, payload) -> Path:
    """Atomically replace ``<data>/cache/steam/<app_id>.json`` with 0600."""
    path = cache_path(data_dir, app_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    return atomic_write_text(path, body, mode=0o600)


def get_achievements(app_id, steam_id, *, data_dir=None, opener=urlopen,
                     api_key=None, use_cache=True, now=None, timeout=REQUEST_TIMEOUT,
                     clock=None, sleep=None):
    """Return a merged Steam source, using the 24 h cache when available.

    The cache stores the merged source, not the raw API payloads, so a cached
    read needs no network and no key. ``None`` means the game has no usable
    Steam achievements.
    """
    if use_cache:
        cached = load_cache(data_dir, app_id, now=now)
        if cached is not None:
            return cached.get("achievement")
    schema = fetch_schema(app_id, opener, api_key=api_key, data_dir=data_dir,
                          timeout=timeout, clock=clock, sleep=sleep)
    player = fetch_player(app_id, steam_id, opener, api_key=api_key, data_dir=data_dir,
                          timeout=timeout, clock=clock, sleep=sleep)
    achievement = merge_schema_player(schema, player)
    save_cache(data_dir, app_id, {
        "app_id": _app_id(app_id),
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "achievement": achievement,
    })
    return achievement


# ── Playtime / last played merge ───────────────────────────────────────────

def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _timestamp(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    parsed = parse_timestamp(value)
    if parsed is None:
        return 0.0
    try:
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def merge_steam_stats(games, owned) -> list:
    """Merge Steam playtime/last-played into games, monotonically.

    ``playtime_seconds`` takes the larger of the local value and Steam minutes
    converted to seconds. ``last_played`` takes the newer timestamp, written
    back as ISO-8601; a zero or missing Steam timestamp never deletes or lowers
    a local value. Games without a matching ``steam_app_id`` are untouched.
    The input list is mutated and returned.
    """
    by_app = {}
    for entry in owned or []:
        if not isinstance(entry, Mapping):
            continue
        app_id = _nonnegative_int(entry.get("appid"))
        if app_id:
            by_app[app_id] = entry
    updated = []
    for game in games or []:
        if not isinstance(game, dict):
            continue
        app_id = _nonnegative_int(game.get("steam_app_id"))
        entry = by_app.get(app_id) if app_id else None
        if entry is None:
            continue
        steam_seconds = _nonnegative_int(entry.get("playtime_forever")) * 60
        local_seconds = _nonnegative_int(game.get("playtime_seconds"))
        game["playtime_seconds"] = max(local_seconds, steam_seconds)
        steam_last = _nonnegative_int(entry.get("rtime_last_played"))
        local_text = str(game.get("last_played") or "")
        if steam_last > 0 and _timestamp(steam_last) > _timestamp(local_text):
            game["last_played"] = datetime.fromtimestamp(steam_last).isoformat(timespec="seconds")
        updated.append(game)
    return updated
