"""Read-only host services for capability plugin hooks (P9.4).

A capability hook may return ``service_requests``; the host executes each
request against this module and re-invokes the hook once with the results.
Everything here is read-only, bounded, resolves no user paths into the
result, and raises ``ValueError`` for unknown or malformed requests so the
caller can record a per-request error instead of trusting plugin input.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import socket
from urllib.parse import urlsplit

MAX_LIBRARY_GAMES = 500
MAX_ACHIEVEMENT_GAMES = 100
MAX_ACHIEVEMENT_ITEMS = 500
MAX_ACTIVITY = 100
MAX_LEADERBOARD = 100
MAX_CATALOG = 20
MAX_CATALOG_DEFAULT = 10
MAX_METADATA = 10
MAX_METADATA_DEFAULT = 5
MAX_MEDIA = 20
MAX_MEDIA_DEFAULT = 10
MAX_URL_LENGTH = 2000

# Keys that could disclose a filesystem layout. Service payloads never carry
# them, even when a provider candidate or achievement item includes them.
_PATH_KEYS = frozenset({
    "path", "paths", "file", "files", "install_dir", "launch",
    "save_paths", "save_sources", "media_root", "screenshot_paths",
})

SERVICES = (
    "library.query",
    "achievements",
    "profile",
    "activity",
    "leaderboards",
    "catalog.search",
    "metadata.search",
    "media.search",
)

# Versioned service descriptors (ADR 0092). Every service is read-only and bounded, and a
# plugin that asks for a version the host does not serve is refused. A v1 plugin never asks
# for a version, so its service calls are unchanged.
SERVICE_DESCRIPTORS = {
    "library.query": {"version": 1, "max_results": MAX_LIBRARY_GAMES, "read_only": True},
    "achievements": {"version": 1, "max_results": MAX_ACHIEVEMENT_ITEMS, "read_only": True},
    "profile": {"version": 1, "max_results": 1, "read_only": True},
    "activity": {"version": 1, "max_results": MAX_ACTIVITY, "read_only": True},
    "leaderboards": {"version": 1, "max_results": MAX_LEADERBOARD, "read_only": True},
    "catalog.search": {"version": 1, "max_results": MAX_CATALOG, "read_only": True},
    "metadata.search": {"version": 1, "max_results": MAX_METADATA, "read_only": True},
    "media.search": {"version": 1, "max_results": MAX_MEDIA, "read_only": True},
}


def describe_services():
    """The service catalog a v2 plugin may rely on: name -> version, bound, read-only flag."""
    return {name: dict(spec) for name, spec in SERVICE_DESCRIPTORS.items()}


def handle_service(service, params, state, *, version=None):
    """Run one allowlisted service and return a bounded JSON object."""
    if service not in SERVICES:
        raise ValueError(f"Unknown plugin service: {service!r}.")
    if version is not None:
        descriptor = SERVICE_DESCRIPTORS.get(service)
        if descriptor is None or str(version) != str(descriptor["version"]):
            raise ValueError(f"Plugin service {service!r} has no version {version}.")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Service parameters must be an object.")
    handler = _HANDLERS[service]
    result = handler(params, state if isinstance(state, dict) else {})
    if not isinstance(result, dict):
        raise ValueError(f"Service {service} produced an invalid result.")
    return _strip_paths(result)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _int_param(params, key, default, maximum, minimum=0):
    value = params.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a whole number.")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a whole number.") from error
    return max(minimum, min(maximum, number))


def _str_param(params, key, default="", maximum=200):
    value = params.get(key, default)
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    return value.strip()[:maximum]


def _games(state):
    games = state.get("games")
    if not isinstance(games, list):
        return []
    return [game for game in games if isinstance(game, dict)]


def _safe_int(value, fallback=0):
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value, fallback=0.0):
    if isinstance(value, bool):
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _strip_paths(value):
    if isinstance(value, dict):
        return {
            str(key): _strip_paths(item)
            for key, item in value.items()
            if str(key) not in _PATH_KEYS
        }
    if isinstance(value, list):
        return [_strip_paths(item) for item in value]
    return value


def _achievement_sources(game):
    """Projected sources when present, with a RetroAchievements fallback.

    The public projection grows ``achievement_sources`` in P11; until then a
    plugin still sees the stored RA totals rather than an empty tab.
    """
    sources = game.get("achievement_sources")
    if isinstance(sources, dict) and sources:
        return sources
    total = _safe_int(game.get("ra_achievements_total"))
    if total <= 0:
        return {}
    return {
        "retroachievements": {
            "name": "RetroAchievements",
            "earned": _safe_int(game.get("ra_achievements_earned")),
            "total": total,
            "progress_pct": _safe_float(game.get("ra_progress_pct")),
            "mastered": bool(game.get("ra_mastered")),
            "items": [],
        }
    }


def _clean_achievement_item(item):
    if not isinstance(item, dict):
        return None
    clean = {}
    for key in ("id", "title", "description", "points", "earned", "icon_url"):
        if key in item and isinstance(item[key], (str, int, float, bool)):
            clean[key] = item[key]
    return clean


# ---------------------------------------------------------------------------
# library.query
# ---------------------------------------------------------------------------

def _library_query(params, state):
    limit = _int_param(params, "limit", MAX_LIBRARY_GAMES, MAX_LIBRARY_GAMES)
    query = _str_param(params, "query", "", 200).casefold()
    platform = _str_param(params, "platform", "", 80)
    rows = []
    for game in _games(state):
        name = str(game.get("name") or "")
        if query and query not in name.casefold():
            continue
        if platform and str(game.get("platform") or "").casefold() != platform.casefold():
            continue
        rows.append({
            "game_id": str(game.get("game_id") or ""),
            "name": name,
            "platform": str(game.get("platform") or ""),
            "progress": str(game.get("progress") or ""),
            "favorite": bool(game.get("favorite")),
            "playtime_seconds": _safe_int(game.get("playtime_seconds")),
            "last_played": str(game.get("last_played") or ""),
            "source": str(game.get("source") or ""),
            "ra_progress_pct": _safe_float(game.get("ra_progress_pct")),
        })
        if len(rows) > limit:
            break
    truncated = len(rows) > limit
    return {"games": rows[:limit], "truncated": truncated}


# ---------------------------------------------------------------------------
# achievements / profile
# ---------------------------------------------------------------------------

def _achievements(params, state):
    game_ids = params.get("game_ids")
    if not isinstance(game_ids, list) or len(game_ids) > MAX_ACHIEVEMENT_GAMES:
        raise ValueError("game_ids must be a list of at most 100 ids.")
    wanted = {item for item in game_ids if isinstance(item, str) and item}
    games = []
    for game in _games(state):
        game_id = str(game.get("game_id") or "")
        if game_id not in wanted:
            continue
        sources = {}
        for name, entry in _achievement_sources(game).items():
            if not isinstance(entry, dict):
                continue
            items = entry.get("items") if isinstance(entry.get("items"), list) else []
            clean = {
                key: entry[key] for key in ("name", "earned", "total", "progress_pct", "mastered")
                if key in entry and isinstance(entry[key], (str, int, float, bool))
            }
            clean["items"] = [item for item in (
                _clean_achievement_item(entry_item)
                for entry_item in items[:MAX_ACHIEVEMENT_ITEMS]
            ) if item]
            if len(items) > MAX_ACHIEVEMENT_ITEMS:
                clean["truncated"] = True
            sources[str(name)] = clean
        games.append({"game_id": game_id, "achievement_sources": sources})
    return {"games": games, "truncated": False}


def _profile(params, state):
    games = _games(state)
    playtime = 0
    earned = 0
    total = 0
    mastered = 0
    favorites = 0
    for game in games:
        playtime += max(0, _safe_int(game.get("playtime_seconds")))
        if game.get("favorite"):
            favorites += 1
        game_mastered = bool(game.get("ra_mastered"))
        for entry in _achievement_sources(game).values():
            if not isinstance(entry, dict):
                continue
            earned += max(0, _safe_int(entry.get("earned")))
            total += max(0, _safe_int(entry.get("total")))
            if entry.get("mastered"):
                game_mastered = True
        if game_mastered:
            mastered += 1
    return {
        "games": len(games),
        "playtime_seconds": playtime,
        "achievements_earned": earned,
        "achievements_total": total,
        "mastered": mastered,
        "favorite_count": favorites,
    }


# ---------------------------------------------------------------------------
# activity / leaderboards
# ---------------------------------------------------------------------------

def _activity(params, state):
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    entries = []
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        entries.append({
            "game_id": str(entry.get("game_id") or ""),
            "name": str(entry.get("game") or entry.get("name") or ""),
            "started": str(entry.get("started") or ""),
            "seconds": _safe_int(entry.get("seconds")),
            "exit_code": _safe_int(entry.get("exit_code")),
        })
        if len(entries) >= MAX_ACTIVITY:
            break
    return {"entries": entries, "truncated": len(history) > MAX_ACTIVITY}


def _leaderboards(params, state):
    limit = _int_param(params, "limit", MAX_LEADERBOARD, MAX_LEADERBOARD)
    from pkg.parity.parity_household import compute_leaderboard

    try:
        board = compute_leaderboard(state, limit=limit)
    except Exception as error:  # household data is optional and untrusted
        raise ValueError(f"Leaderboard is unavailable: {error}") from error
    entries = board.get("entries") if isinstance(board, dict) else None
    if not isinstance(entries, list):
        entries = []
    return {
        "available": bool(board.get("available", True)) if isinstance(board, dict) else False,
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


# ---------------------------------------------------------------------------
# catalog / metadata / media
# ---------------------------------------------------------------------------

def _search_catalog(query, platform, limit):
    """Search the local LaunchBox metadata database (patched in tests)."""
    from metadata import search_games
    from webapp_state import METADATA_DATABASE

    if not Path(METADATA_DATABASE).is_file():
        raise ValueError("Metadata database is not available.")
    return search_games(METADATA_DATABASE, query, platform=platform, limit=limit)


def _catalog_search(params, state):
    query = _str_param(params, "query", "", 200)
    if not query:
        raise ValueError("catalog.search needs a query.")
    platform = _str_param(params, "platform", "", 80)
    limit = _int_param(params, "limit", MAX_CATALOG_DEFAULT, MAX_CATALOG)
    try:
        results = _search_catalog(query, platform, limit)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Catalog search failed: {error}") from error
    if not isinstance(results, list):
        results = []
    candidates = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        candidates.append({
            key: item.get(key) for key in (
                "database_id", "name", "platform", "year", "developer",
                "publisher", "genre", "overview", "image_url",
            ) if key in item
        })
    return {"candidates": candidates, "truncated": len(results) > limit}


def _metadata_search_all(state, game, limit):
    """Call the P8 provider registry (patched in tests)."""
    from pkg.parity.parity_metadata_providers import search_all

    return search_all(state, dict(game), limit=limit)


def _metadata_search(params, state):
    limit = _int_param(params, "limit", MAX_METADATA_DEFAULT, MAX_METADATA)
    game = params.get("game")
    if game is not None and not isinstance(game, dict):
        raise ValueError("game must be an object.")
    if game is None:
        query = _str_param(params, "query", "", 200)
        if not query:
            raise ValueError("metadata.search needs a game or query.")
        game = {"name": query}
    try:
        results = _metadata_search_all(state, game, limit)
    except ImportError as error:
        raise ValueError("Metadata providers are unavailable.") from error
    if not isinstance(results, list):
        results = []
    candidates = []
    for candidate in results[:limit]:
        if not isinstance(candidate, dict):
            continue
        media = candidate.get("media") if isinstance(candidate.get("media"), dict) else {}
        clean_media = {
            str(kind): [url for url in urls if isinstance(url, str)][:10]
            for kind, urls in media.items()
            if isinstance(urls, list)
        }
        candidates.append({
            "id": candidate.get("id"),
            "source": candidate.get("source"),
            "name": candidate.get("name"),
            "platform": candidate.get("platform"),
            "year": candidate.get("year"),
            "fields": candidate.get("fields") if isinstance(candidate.get("fields"), dict) else {},
            "media": clean_media,
        })
    return {"candidates": candidates, "truncated": len(results) > limit}


def _media_kinds():
    try:
        from pkg.state.media_probe import MEDIA_PATH_FIELDS, MEDIA_TYPES_ALL
        return frozenset(MEDIA_PATH_FIELDS) | frozenset(MEDIA_TYPES_ALL) | {"screenshots"}
    except Exception:  # an unavailable media module must not block services
        return frozenset({
            "cover", "background", "screenshots", "clear_logo", "fanart",
            "banner", "icon", "box_back", "box_spine", "box_3d",
            "title_screen", "cart_front", "cart_back", "disc", "advertisement",
            "manual", "video", "music", "video_snap", "video_theme",
            "video_trailer",
        })


def _media_candidates(state, game, kind, limit):
    """Built-in media candidates for one kind (patched in tests)."""
    try:
        from pkg.parity.parity_metadata_providers import search_all
    except ImportError:
        return []
    candidates = search_all(state, dict(game), limit=limit)
    if not isinstance(candidates, list):
        return []
    urls = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        media = candidate.get("media") if isinstance(candidate.get("media"), dict) else {}
        values = media.get(kind)
        if not isinstance(values, list):
            continue
        for url in values:
            urls.append({"url": url, "title": candidate.get("name", "")})
    return urls[: limit * 4]


def _media_search(params, state):
    game_id = _str_param(params, "game_id", "", 160)
    if not game_id:
        raise ValueError("media.search needs a game_id.")
    kind = _str_param(params, "kind", "cover", 40)
    if kind not in _media_kinds():
        raise ValueError(f"Unsupported media kind: {kind!r}.")
    limit = _int_param(params, "limit", MAX_MEDIA_DEFAULT, MAX_MEDIA)
    game = next(
        (candidate for candidate in _games(state) if str(candidate.get("game_id") or "") == game_id),
        None,
    )
    if game is None:
        raise ValueError("Unknown game_id.")
    candidates = _media_candidates(state, game, kind, limit)
    if not isinstance(candidates, list):
        candidates = []
    clean = []
    for candidate in candidates[:limit]:
        url = candidate.get("url") if isinstance(candidate, dict) else candidate
        if not isinstance(url, str):
            continue
        try:
            safe = safe_media_url(url)
        except ValueError:
            continue
        item = {"url": safe, "kind": kind}
        if isinstance(candidate, dict):
            for key in ("title", "width", "height"):
                if key in candidate and isinstance(candidate[key], (str, int, float)):
                    item[key] = candidate[key]
        clean.append(item)
    return {"candidates": clean, "truncated": len(candidates) > limit}


def safe_media_url(url):
    """Return *url* when it is a public HTTPS URL; otherwise raise.

    Rejects credentials in the URL, loopback/private/link-local targets, and
    hosts that cannot be resolved. Downloading is the caller's job; this is
    the SSRF gate before any fetch.
    """
    if not isinstance(url, str):
        raise ValueError("Media URL must be text.")
    value = url.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise ValueError("Media URL is empty or too long.")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError("Media URLs must use HTTPS.")
    if parts.username or parts.password:
        raise ValueError("Media URLs may not carry credentials.")
    host = parts.hostname
    if not host:
        raise ValueError("Media URL needs a host.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as error:
        raise ValueError("Media host could not be resolved.") from error
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise ValueError("Media host resolved to an invalid address.") from error
        if (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified):
            raise ValueError("Media host resolves to a non-public address.")
    return value


_HANDLERS = {
    "library.query": _library_query,
    "achievements": _achievements,
    "profile": _profile,
    "activity": _activity,
    "leaderboards": _leaderboards,
    "catalog.search": _catalog_search,
    "metadata.search": _metadata_search,
    "media.search": _media_search,
}
