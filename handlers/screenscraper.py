"""ScreenScraperHandlers — per-ROM-hash metadata/media provider (1.8.0, ADR 0022).

Routes are additive v2 only. Credentials live in ~/.env (SCREENSCRAPER_USER /
SCREENSCRAPER_PASSWORD), never in settings JSON. Scraping is user-triggered,
except the opt-in post-import auto-scrape pass (settings
scrape_screenscraper_enabled), which only auto-applies dual-hash confident
matches. The batch hash-match job is cancellable from the Activity
Center and rate-limited by the parity module.
"""

import copy
import re
from pathlib import Path
from urllib.parse import parse_qs

from openbox import DATA, load_state
from parity_premium import download_bytes
from pkg.parity.parity_screenscraper import (
    HASH_TIER_DUAL,
    HASH_TIER_NONE,
    HASH_TIER_TITLE,
    apply_to_game,
    auto_apply_allowed,
    cache_size,
    choose_media,
    clean_media_url,
    confident_hash_match,
    game_info,
    is_configured,
    search_games,
    system_id_for_platform,
)
from routes.registry import route
from webapp_state import JOB_MANAGER, game_from_payload, transact_state

MATCH_BATCH_LIMIT = 100
_EXACT_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _clean_exact_media_urls(value, media_kinds, allowed):
    """Validate an exact-candidate ``{kind: url}`` map for thumbnail chooser apply.

    Only http(s) URLs for chosen media kinds that appear in ``allowed``
    (the candidate set for the chosen ScreenScraper game) survive; anything
    else is dropped so the client cannot pick an arbitrary download URL.
    """
    cleaned = {}
    if isinstance(value, dict):
        for kind, url in value.items():
            kind = str(kind or "").strip()
            url = str(url or "").strip()
            if kind in media_kinds and _EXACT_URL_RE.match(url) and (kind, url) in allowed:
                # choose_media yields a list for screenshots; accept one URL there too.
                cleaned[kind] = [url] if kind == "screenshots" else url
    return cleaned
_MEDIA_EXT = {"video_snap": ".mp4", "manual": ".pdf"}


def _cache_dir():
    return DATA.parent / "cache"


class ScreenScraperHandlers:
    @route("GET", "/api/v2/screenscraper/status")
    def _api_get_api_v2_screenscraper_status(self, parsed):
        self.send_json(200, {
            "configured": is_configured(),
            "cache_entries": cache_size(_cache_dir()),
        })
        return

    @route("POST", "/api/v2/screenscraper/test")
    def _api_post_api_v2_screenscraper_test(self, payload):
        from api_errors import BadRequest
        from pkg.parity.parity_screenscraper import user_info

        try:
            info = user_info(cache_dir=_cache_dir())
        except ValueError as error:
            raise BadRequest(str(error)) from None
        response = info.get("response") or {}
        user = (response.get("ssuser") or {})
        self.send_json(200, {"ok": True, "user": user})
        return

    @route("GET", "/api/v2/screenscraper/search")
    def _api_get_api_v2_screenscraper_search(self, parsed):
        from api_errors import BadRequest

        qs = parse_qs(parsed.query or "")
        query = (qs.get("q", [""])[0] or "").strip()
        platform = (qs.get("platform", [""])[0] or "").strip()
        try:
            results = search_games(query, system_id=system_id_for_platform(platform))
        except ValueError as error:
            raise BadRequest(str(error)) from None
        self.send_json(200, {"results": results})
        return

    @route("POST", "/api/v2/screenscraper/info")
    def _api_post_api_v2_screenscraper_info(self, payload):
        """Fetch metadata for one game (by scraper id or a ROM path) for review."""
        from api_errors import BadRequest

        scraper_id = payload.get("scraper_id")
        rom_path = str(payload.get("rom_path", "")).strip()
        system_id = system_id_for_platform(payload.get("platform", ""))
        try:
            metadata = game_info(
                scraper_id,
                rom_path=rom_path or None,
                system_id=system_id,
                cache_dir=_cache_dir(),
            )
        except ValueError as error:
            raise BadRequest(str(error)) from None
        self.send_json(200, metadata)
        return

    @route("POST", "/api/v2/screenscraper/match")
    def _api_post_api_v2_screenscraper_match(self, payload):
        self.start_hash_match(payload)

    @route("POST", "/api/v2/screenscraper/apply")
    def _api_post_api_v2_screenscraper_apply(self, payload):
        self.start_apply(payload)

    def start_hash_match(self, payload):
        ids = payload.get("ids") if isinstance(payload.get("ids"), list) else []
        ids = [str(item) for item in ids][:MATCH_BATCH_LIMIT]
        apply_confident = bool(payload.get("apply_confident"))
        system_ids = {str(game.get("game_id")): system_id_for_platform(game.get("platform")) for game in load_state().get("games", [])}

        def worker(cancel_event):
            state = load_state()
            games = state.get("games", []) or []
            if ids:
                games = [game for game in games if str(game.get("game_id")) in set(ids)]
            results = []
            for game in games[:MATCH_BATCH_LIMIT]:
                if cancel_event is not None and cancel_event.is_set():
                    break
                stable_id = str(game.get("game_id") or "")
                entry = {"game_id": stable_id, "name": game.get("name", "")}
                rom_path = str(game.get("path", "") or "")
                had_rom = bool(rom_path and Path(rom_path).is_file())
                metadata, tier = None, HASH_TIER_NONE
                if had_rom:
                    try:
                        metadata, tier = confident_hash_match(
                            rom_path,
                            system_id=system_ids.get(stable_id),
                            cache_dir=_cache_dir(),
                        )
                    except ValueError as error:
                        entry["hash_error"] = str(error)
                if tier == HASH_TIER_DUAL:
                    # Hash tier wins outright; never falls through to title.
                    entry.update({
                        "status": "matched",
                        "matched_by": "hash",
                        "confidence": tier,
                        "scraper_id": metadata.get("id"),
                        "match_name": metadata.get("name", ""),
                    })
                    if apply_confident and auto_apply_allowed(tier):
                        _apply_hash_match(stable_id, metadata)
                        entry["applied"] = True
                else:
                    # No confident hash evidence: fall through to title
                    # matching. Title matches are always review-only.
                    title_hit = _title_fallback(game, system_ids.get(stable_id))
                    if title_hit is not None:
                        entry.update({
                            "status": "matched",
                            "matched_by": "title",
                            "confidence": HASH_TIER_TITLE,
                            "scraper_id": title_hit.get("id"),
                            "match_name": title_hit.get("name", ""),
                        })
                    else:
                        entry["status"] = "not_found" if had_rom else "no_rom"
                        entry["confidence"] = HASH_TIER_NONE
                results.append(entry)
            return {"matches": results, "count": len(results)}

        job = JOB_MANAGER.submit("screenscraper-match", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def start_apply(self, payload):
        from api_errors import BadRequest

        stable_id = str(payload.get("id", "")).strip()
        if not stable_id:
            raise BadRequest("A stable game id is required.")
        scraper_id = payload.get("scraper_id")
        rom_path = str(payload.get("rom_path", "")).strip()
        fields = payload.get("fields") if isinstance(payload.get("fields"), list) else ["name", "description", "year", "genre", "developer", "publisher"]
        media_kinds = payload.get("media") if isinstance(payload.get("media"), list) else []
        replace_existing = bool(payload.get("replace_existing"))

        def worker(_cancel_event):
            state = load_state()
            original = copy.deepcopy(game_from_payload(state, {"game_id": stable_id}))
            system_id = system_id_for_platform(original.get("platform"))
            metadata = game_info(
                scraper_id,
                rom_path=rom_path or None,
                system_id=system_id,
                cache_dir=_cache_dir(),
            )
            media_urls = choose_media(
                metadata,
                [str(kind) for kind in media_kinds],
                region_priority=state.get("settings", {}).get("region_priority"),
            )
            # Exact-candidate apply from the thumbnail chooser wins over the top
            # pick — but only for URLs the provider returned for this game id,
            # so the client cannot make the server fetch an arbitrary URL.
            allowed = {
                (str(entry.get("kind") or "").strip(), str(entry.get("url") or "").strip())
                for entry in (metadata.get("media") or [])
                if isinstance(entry, dict) and entry.get("url")
            }
            media_urls.update(_clean_exact_media_urls(payload.get("media_urls"), [str(kind) for kind in media_kinds], allowed))
            slug = "".join(char if char.isalnum() or char in "-_ " else "" for char in str(original.get("name") or stable_id)).strip().replace(" ", "-")[:60] or stable_id
            media_root = DATA.parent / "media" / "screenscraper" / slug
            downloaded = {}
            for kind, url in media_urls.items():
                url = clean_media_url(url)
                if not url:
                    continue
                field = "screenshots" if kind == "screenshots" else kind
                if not replace_existing and original.get(field):
                    continue
                if kind == "screenshots":
                    paths = []
                    for index, shot_url in enumerate(url):
                        try:
                            paths.append(download_bytes(shot_url, media_root / "screenshots" / f"ss-{index}{_ext_for(shot_url, '.jpg')}"))
                        except (OSError, ValueError):
                            continue
                    if paths:
                        downloaded[field] = paths
                    continue
                try:
                    downloaded[field] = download_bytes(url, media_root / f"{kind}{_ext_for(url, _MEDIA_EXT.get(kind, '.jpg'))}")
                except (OSError, ValueError):
                    continue

            def mutate(state):
                game = game_from_payload(state, {"game_id": stable_id})
                apply_to_game(game, metadata, fields=[str(field) for field in fields])
                for field, value in downloaded.items():
                    game[field] = value
                return game.get("name", "")

            _, name = transact_state(mutate)
            return {"applied": True, "game": name, "media": sorted(downloaded)}

        job = JOB_MANAGER.submit("screenscraper-apply", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})


def _ext_for(url, default):
    candidate = Path(str(url).split("?")[0]).suffix.casefold()
    return candidate if candidate else default


def _title_fallback(game, system_id):
    """Best-effort title match for a game with no confident hash evidence.

    Returns the top search hit or None. Callers treat title matches as
    review-only; they are never auto-applied.
    """
    name = str(game.get("name") or "").strip()
    if not name:
        return None
    try:
        results = search_games(name, system_id=system_id, limit=5)
    except (OSError, ValueError):
        return None
    return results[0] if results else None


def _apply_hash_match(stable_id, metadata):
    """Apply a dual-confident hash match, recording hash provenance."""
    def mutate(state):
        game = game_from_payload(state, {"game_id": stable_id})
        apply_to_game(game, metadata)
        game["screenscraper_id"] = metadata.get("id")
        game["matched_by"] = "hash"
        game["match_confidence"] = HASH_TIER_DUAL
        return game.get("name", "")

    _, name = transact_state(mutate)
    return name
