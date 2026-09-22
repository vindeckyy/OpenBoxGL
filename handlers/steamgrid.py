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
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pkg.parity  # noqa: F401,E402  # installs the flat parity_* import finder
import openbox  # noqa: E402
from api_errors import BadRequest  # noqa: E402
from parity_premium import download_bytes  # noqa: E402
from pkg.parity.parity_artwork_hygiene import (  # noqa: E402
    PROVIDER_ATTRIBUTION,
    build_report,
    list_undo_batches,
    load_undo_manifest,
    select_fixable,
    snapshot_for_replacement,
    undo_batch,
    write_undo_manifest,
)
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
HYGIENE_BATCH_LIMIT = 200
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


def _clean_exact_media_urls(value, allowed):
    """Validate an exact-candidate ``{kind: url}`` map for thumbnail chooser apply.

    Only http(s) URLs for known media kinds that appear in ``allowed``
    (the candidate set for the chosen SteamGridDB game) survive; anything
    else is dropped so the client cannot pick an arbitrary download URL.
    """
    cleaned = {}
    if isinstance(value, dict):
        for kind, url in value.items():
            kind = str(kind or "").strip()
            url = str(url or "").strip()
            if kind in _DEFAULT_MEDIA_KINDS and re.match(r"^https?://\S+$", url, re.IGNORECASE) and (kind, url) in allowed:
                cleaned[kind] = url
    return cleaned


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
        # Exact-candidate apply from the thumbnail chooser wins over the top
        # pick — but only for URLs the provider returned for this game id, so
        # the client cannot make the server fetch an arbitrary URL.
        allowed = {
            (str(entry.get("kind") or "").strip(), str(entry.get("url") or "").strip())
            for entry in (metadata.get("media") or [])
            if isinstance(entry, dict) and entry.get("url")
        }
        media_urls.update(_clean_exact_media_urls(payload.get("media_urls"), allowed))
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


@route("GET", "/api/v2/steamgrid/hygiene/report", spec="handlers.steamgrid.steamgrid_hygiene_report")
def steamgrid_hygiene_report(handler, parsed):
    """Read-only Artwork Doctor report: gaps, resolution, aspect, duplicates."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    query = parse_qs(parsed.query or "")
    limit_raw = (query.get("limit", [""])[0] or "").strip()
    media_root = openbox.DATA.parent / "media"
    games = openbox.load_state().get("games", []) or []
    if limit_raw.isdigit():
        games = games[: max(1, int(limit_raw))]
    report = build_report(games, media_root=media_root)
    report["batches"] = list_undo_batches(_cache_dir())
    handler.send_json(200, report)


@route("POST", "/api/v2/steamgrid/hygiene/fix", spec="handlers.steamgrid.steamgrid_hygiene_fix")
def steamgrid_hygiene_fix(handler, payload):
    """Queue the "fix all with SteamGridDB" batch job (cancelable, undoable)."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    _require_available(_settings())
    payload = payload if isinstance(payload, dict) else {}
    game_ids = payload.get("game_ids") if isinstance(payload.get("game_ids"), list) else []
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else ["cover"]
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else None
    limit = payload.get("limit")
    try:
        limit = max(1, min(int(limit), HYGIENE_BATCH_LIMIT)) if limit is not None else HYGIENE_BATCH_LIMIT
    except (TypeError, ValueError):
        raise BadRequest("limit must be an integer.") from None

    def worker(cancel_event):
        state = openbox.load_state()
        games = state.get("games", []) or []
        report = build_report(games, media_root=openbox.DATA.parent / "media")
        targets = select_fixable(
            report,
            issues=[str(item) for item in issues] if issues else None,
            fields=[str(item) for item in fields],
            game_ids=[str(item) for item in game_ids] if game_ids else None,
        )[:limit]
        batch_id = uuid.uuid4().hex
        records = []
        results = []
        for index, target in enumerate(targets):
            if cancel_event is not None and cancel_event.is_set():
                break
            if cancel_event is not None:
                cancel_event.progress(processed=index, current=index, total=len(targets), applied=len(records))
            entry = {"game_id": target["game_id"], "name": target["name"], "field": target["field"]}
            game = next((item for item in games if str(item.get("game_id")) == target["game_id"]), None)
            if game is None:
                entry["status"] = "game_missing"
                results.append(entry)
                continue
            try:
                _hygiene_replace_one(game, target, batch_id, records, entry)
            except ValueError as error:
                entry["status"] = "error"
                entry["error"] = str(error)
            results.append(entry)
        if records:
            write_undo_manifest(_cache_dir(), batch_id, records, provider=PROVIDER_ATTRIBUTION)
        applied = sum(1 for entry in results if entry.get("status") == "applied")
        return {
            "batch_id": batch_id,
            "provider": PROVIDER_ATTRIBUTION,
            "applied": applied,
            "failed": len(results) - applied,
            "total": len(targets),
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
            "results": results,
        }

    job = JOB_MANAGER.submit("steamgrid-hygiene-fix", worker)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"], "provider": PROVIDER_ATTRIBUTION})


def _hygiene_replace_one(game, target, batch_id, records, entry):
    stable_id = target["game_id"]
    field = target["field"]
    name = str(game.get("name") or "").strip()
    if not name:
        raise ValueError("This game has no name to search with.")
    found = search_games(name, limit=1, cache_dir=_cache_dir())
    if not found:
        entry["status"] = "not_found"
        return
    sgd_id = found[0].get("id")
    entry["steamgrid_id"] = sgd_id
    urls = choose_media({"media": game_assets(sgd_id, cache_dir=_cache_dir())}, [field])
    media_url = clean_media_url(urls.get(field, ""))
    if not media_url:
        entry["status"] = "not_found"
        return
    previous = str(game.get(field) or "")
    media_root = _media_root(game, stable_id)
    record = snapshot_for_replacement(_cache_dir(), batch_id, stable_id, field, previous)
    downloaded = download_bytes(media_url, media_root / f"{field}{_ext_for(media_url, '.png')}")
    record["new"] = str(downloaded)

    def mutate(state):
        target_game = game_from_payload(state, {"game_id": stable_id})
        target_game[field] = str(downloaded)
        target_game["artwork_provider"] = PROVIDER_ATTRIBUTION
        target_game["steamgrid_id"] = sgd_id

    transact_state(mutate)
    records.append(record)
    bump_media_epoch()
    entry["status"] = "applied"
    entry["media"] = str(downloaded)


@route("POST", "/api/v2/steamgrid/hygiene/undo", spec="handlers.steamgrid.steamgrid_hygiene_undo")
def steamgrid_hygiene_undo(handler, payload):
    """Restore artwork replaced by a hygiene batch and clear new files."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    batch_id = str((payload or {}).get("batch_id") or "").strip()
    if not batch_id:
        raise BadRequest("batch_id is required.")
    try:
        manifest = load_undo_manifest(_cache_dir(), batch_id)
        restored = undo_batch(_cache_dir(), batch_id)
    except ValueError as error:
        raise BadRequest(str(error), code="ARTWORK_UNDO_MISSING") from None
    previous_by_key = {
        (str(record.get("game_id")), str(record.get("field"))): str(record.get("previous") or "")
        for record in manifest["records"]
        if isinstance(record, dict)
    }
    counts = {"removed": 0, "restored": 0}

    def mutate(state):
        for record in restored:
            field = str(record.get("field") or "")
            stable_id = str(record.get("game_id") or "")
            if not field or not stable_id:
                continue
            try:
                game = game_from_payload(state, {"game_id": stable_id})
            except BadRequest:
                continue
            if record.get("action") == "restored":
                previous = previous_by_key.get((stable_id, field), "")
                if previous:
                    game[field] = previous
                    counts["restored"] += 1
            elif record.get("action") == "removed":
                game[field] = ""
                counts["removed"] += 1

    transact_state(mutate)
    bump_media_epoch()
    handler.send_json(200, {"batch_id": batch_id, "restored": restored, "counts": counts})


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
