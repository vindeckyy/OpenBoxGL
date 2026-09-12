"""SteamGridDB v2 routes (S4).

Additive v2 surface mirroring handlers/screenscraper.py: status, search,
info (media review), apply (per-game artwork job), match (bulk fill for
games missing a cover), and test (credential probe). The API key lives in
~/.env (STEAMGRIDDB_API_KEY), never in settings JSON; ``steamgrid_enabled``
toggles the provider. Functions use dotted specs like handlers/resume.py,
so no web_app.py Handler change is needed.
"""

from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path
from urllib.parse import parse_qs

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pkg.parity  # noqa: F401,E402  # installs the flat parity_* import finder
import openbox  # noqa: E402
from api_errors import BadRequest  # noqa: E402
from parity_premium import download_bytes  # noqa: E402
from pkg.parity.parity_steamgrid import (  # noqa: E402
    apply_to_game,
    choose_media,
    clean_media_url,
    enabled as steamgrid_enabled,
    game_assets,
    game_info,
    is_configured,
    provider_available,
    provider_status,
    search_games,
)
from routes.registry import route  # noqa: E402
from webapp_state import JOB_MANAGER, bump_media_epoch, game_from_payload, transact_state  # noqa: E402

MATCH_BATCH_LIMIT = 100
_DEFAULT_MEDIA_KINDS = ("cover", "background", "clear_logo", "icon", "banner")


def _cache_dir():
    return openbox.DATA.parent / "cache"


def _settings():
    return openbox.load_state().get("settings", {})


def _require_available(settings):
    if provider_available(settings):
        return
    if not steamgrid_enabled(settings):
        raise BadRequest("SteamGridDB provider is disabled in Settings.", code="STEAMGRID_DISABLED")
    if not is_configured():
        raise BadRequest("Set STEAMGRIDDB_API_KEY in ~/.env to use SteamGridDB.", code="STEAMGRID_UNCONFIGURED")
    raise BadRequest("SteamGridDB rejected the API key; provider disabled until restart.", code="STEAMGRID_KEY_REJECTED")


def _media_kinds(payload):
    kinds = payload.get("media") if isinstance(payload.get("media"), list) else None
    if not kinds:
        return list(_DEFAULT_MEDIA_KINDS)
    return [str(kind) for kind in kinds]


def _slug_for(name, fallback):
    slug = "".join(char if char.isalnum() or char in "-_ " else "" for char in str(name or "")).strip().replace(" ", "-")[:60]
    return slug or fallback


def _media_root(game, fallback="game"):
    """Return a title-readable but identity-unique artwork directory."""
    stable_id = str((game or {}).get("game_id") or fallback or "game").strip()
    digest = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]
    slug = _slug_for((game or {}).get("name"), stable_id)
    return openbox.DATA.parent / "media" / "steamgrid" / f"{slug}-{digest}"


def _ext_for(url, default):
    candidate = Path(str(url).split("?")[0]).suffix.casefold()
    return candidate if candidate else default


def _download_media(media_urls, original, media_root, replace_existing):
    downloaded = {}
    for kind, url in media_urls.items():
        url = clean_media_url(url)
        if not url:
            continue
        if not replace_existing and original.get(kind):
            continue
        try:
            downloaded[kind] = download_bytes(url, media_root / f"{kind}{_ext_for(url, '.png')}")
        except (OSError, ValueError):
            continue
    return downloaded


@route("GET", "/api/v2/steamgrid/status", spec="handlers.steamgrid.steamgrid_status")
def steamgrid_status(handler, parsed):
    """Report provider configuration/toggle/session state + cache size."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    handler.send_json(200, provider_status(_settings(), cache_dir=_cache_dir()))


@route("GET", "/api/v2/steamgrid/search", spec="handlers.steamgrid.steamgrid_search")
def steamgrid_search(handler, parsed):
    """Autocomplete search; results carry provider="steamgrid" for merging."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    _require_available(_settings())
    query = parse_qs(parsed.query or "")
    name = (query.get("q", [""])[0] or "").strip()
    limit = (query.get("limit", [""])[0] or "").strip() or 12
    try:
        results = search_games(name, limit=limit, cache_dir=_cache_dir())
    except ValueError as error:
        raise BadRequest(str(error)) from None
    handler.send_json(200, {"results": results, "provider": "steamgrid"})


@route("POST", "/api/v2/steamgrid/test", spec="handlers.steamgrid.steamgrid_test")
def steamgrid_test(handler, payload):
    """Probe the configured key with one bounded autocomplete request."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    _require_available(_settings())
    try:
        results = search_games("steam", limit=1)
    except ValueError as error:
        raise BadRequest(str(error)) from None
    handler.send_json(200, {"ok": True, "probe_results": len(results)})


@route("POST", "/api/v2/steamgrid/info", spec="handlers.steamgrid.steamgrid_info")
def steamgrid_info(handler, payload):
    """Fetch normalized metadata + media for one SGDB id (apply preview)."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    _require_available(_settings())
    steamgrid_id = payload.get("steamgrid_id")
    if steamgrid_id is None:
        raise BadRequest("steamgrid_id is required.")
    try:
        metadata = game_info(steamgrid_id, cache_dir=_cache_dir())
    except ValueError as error:
        raise BadRequest(str(error)) from None
    handler.send_json(200, metadata)


@route("POST", "/api/v2/steamgrid/apply", spec="handlers.steamgrid.steamgrid_apply")
def steamgrid_apply(handler, payload):
    """Queue an apply job: metadata fields + chosen media kinds onto a game."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    _require_available(_settings())
    stable_id = str(payload.get("id", "")).strip()
    if not stable_id:
        raise BadRequest("A stable game id is required.")
    steamgrid_id = payload.get("steamgrid_id")
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else ["name", "year"]
    media_kinds = _media_kinds(payload)
    replace_existing = bool(payload.get("replace_existing"))

    def worker(_cancel_event):
        state = openbox.load_state()
        original = copy.deepcopy(game_from_payload(state, {"game_id": stable_id}))
        sgd_id = steamgrid_id
        if sgd_id is None:
            found = search_games(original.get("name", ""), limit=1, cache_dir=_cache_dir())
            if not found:
                return {"applied": False, "game": original.get("name", ""), "error": "no_match"}
            sgd_id = found[0].get("id")
        metadata = game_info(sgd_id, cache_dir=_cache_dir())
        media_urls = choose_media(metadata, media_kinds)
        media_root = _media_root(original, stable_id)
        downloaded = _download_media(media_urls, original, media_root, replace_existing)

        def mutate(state):
            game = game_from_payload(state, {"game_id": stable_id})
            apply_to_game(game, metadata, fields=[str(field) for field in fields])
            for field, value in downloaded.items():
                game[field] = value
            return game.get("name", "")

        _, name = transact_state(mutate)
        bump_media_epoch()
        return {"applied": True, "game": name, "media": sorted(downloaded)}

    job = JOB_MANAGER.submit("steamgrid-apply", worker)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"]})


@route("POST", "/api/v2/steamgrid/match", spec="handlers.steamgrid.steamgrid_match")
def steamgrid_match(handler, payload):
    """Queue a bulk fill job: games missing a cover (or an explicit id set)."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    _require_available(_settings())
    ids = payload.get("ids") if isinstance(payload.get("ids"), list) else []
    ids = [str(item) for item in ids][:MATCH_BATCH_LIMIT]
    media_kinds = _media_kinds(payload)

    def worker(cancel_event):
        state = openbox.load_state()
        games = state.get("games", []) or []
        if ids:
            games = [game for game in games if str(game.get("game_id")) in set(ids)]
        else:
            games = [game for game in games if not game.get("cover")]
        results = []
        for game in games[:MATCH_BATCH_LIMIT]:
            if cancel_event is not None and cancel_event.is_set():
                break
            entry = {"game_id": str(game.get("game_id") or ""), "name": game.get("name", "")}
            name = str(game.get("name") or "").strip()
            if not name:
                entry["status"] = "no_name"
                results.append(entry)
                continue
            try:
                _match_one(game, name, media_kinds, entry)
            except ValueError as error:
                entry["status"] = "error"
                entry["error"] = str(error)
            results.append(entry)
        bump_media_epoch()
        return {"matches": results, "count": len(results)}

    job = JOB_MANAGER.submit("steamgrid-match", worker)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"]})


def _match_one(game, name, media_kinds, entry):
    found = search_games(name, limit=1, cache_dir=_cache_dir())
    if not found:
        entry["status"] = "not_found"
        return
    sgd_id = found[0].get("id")
    entry["steamgrid_id"] = sgd_id
    entry["match_name"] = found[0].get("name", "")
    urls = choose_media({"media": game_assets(sgd_id, cache_dir=_cache_dir())}, media_kinds)
    media_root = _media_root(game, entry["game_id"])
    downloaded = _download_media(urls, game, media_root, False)
    if downloaded:
        stable_id = entry["game_id"]

        def mutate(state):
            target = game_from_payload(state, {"game_id": stable_id})
            for field, value in downloaded.items():
                target[field] = value
            target["steamgrid_id"] = sgd_id

        transact_state(mutate)
    entry["status"] = "matched"
    entry["media"] = sorted(downloaded)
