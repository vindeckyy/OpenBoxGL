"""MetadataHandlers capability handlers. Metadata search, status, apply, sync, Steam, IGDB, and batch auto-match."""

import copy
import re
import sqlite3
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadRequest, Conflict, PreviewExpired, PreviewNotFound
from metadata import (
    DEFAULT_MATCH_ITEMS_LIMIT,
    MAX_MATCH_ITEMS_LIMIT,
    IMAGE_URL,
    MEDIA_TYPE_MAP,
    apply_game_metadata,
    apply_match_decisions,
    apply_match_preview,
    batch_match,
    create_match_preview_record,
    list_match_preview_items,
    load_match_preview,
    match_preview_document,
    persist_never_rejections,
    run_match_preview_job,
    save_match_preview,
    search_games,
    sync_database,
)
from openbox import load_state
from parity_backup import create_backup
from parity_igdb import apply_to_game as apply_igdb_metadata, credentials as igdb_credentials, fetch_game as fetch_igdb_game, search_games as search_igdb_games
from pkg.parity.parity_screenscraper import (
    HASH_TIER_DUAL,
    apply_to_game as apply_screenscraper_metadata,
    confident_hash_match,
    is_configured as screenscraper_configured,
    system_id_for_platform,
)
from pkg.parity.parity_steamgrid import (
    choose_media as choose_steamgrid_media,
    clean_media_url as clean_steamgrid_url,
    game_info as steamgrid_game_info,
    is_configured as steamgrid_configured,
    search_games as search_steamgrid_games,
)
from parity_premium import download_bytes
from pkg.state.operations import get_operation_service
from routes.registry import route
from webapp_state import DATA, JOB_MANAGER, METADATA_DATABASE, METADATA_JOB, MEDIA_TYPES_ALL, PROCESS_LOCK, RUNNING, bump_media_epoch, game_from_payload, game_from_query, load_state_view, transact_state, update_steam_metadata


def _parse_limit(raw, *, default: int, maximum: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise BadRequest("limit must be an integer.") from error
    return max(1, min(value, maximum))


def _match_preview_state_for_job(job_id: str | None) -> str:
    if not job_id:
        return "ready"
    operation = get_operation_service().get(job_id)
    if not operation:
        return "ready"
    state = str(operation.get("state") or "ready")
    if state in {"queued", "running", "cancelling"}:
        return state
    if state in {"done", "partial", "error"}:
        return "ready"
    return state


class MetadataHandlers:
    @route("GET", "/api/metadata/status")
    def _api_get_api_metadata_status(self, parsed):
        with PROCESS_LOCK:
            job = dict(METADATA_JOB)
        state_view = load_state_view()
        games = state_view["games"]
        matched = sum(bool(game.get("launchbox_db_id")) for game in games)
        def _missing(field):
            return sum(not Path(str(game.get(field) or "")).is_file() for game in games)
        coverage = {
            "games": len(games),
            "matched_games": matched,
            "matched_ratio": round(matched / len(games), 4) if games else 0.0,
        }
        for field in sorted(MEDIA_TYPES_ALL):
            coverage[f"with_{field}"] = len(games) - _missing(field)
        self.send_json(200, {"ready":METADATA_DATABASE.is_file(), "job":job, "coverage":coverage})
        return

    @route("GET", "/api/metadata/search")
    def _api_get_api_metadata_search(self, parsed):
        if not METADATA_DATABASE.is_file():
            raise Conflict("Download the LaunchBox metadata database first.")
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            title = query.get("q", [game.get("name", "")])[0]
            results = search_games(METADATA_DATABASE, title, game.get("platform", ""))
            self.send_json(200, {"results":results})
        except (KeyError, IndexError, ValueError, sqlite3.Error) as error:
            raise BadRequest(str(error)) from None
        return

    @route("GET", "/api/metadata/igdb/search")
    def _api_get_api_metadata_igdb_search(self, parsed):
        query = parse_qs(parsed.query).get("q", [""])[0]
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        try:
            results = search_igdb_games(query, platform=platform)
        except (OSError, ValueError) as error:
            raise BadRequest(str(error)) from None
        self.send_json(200, {"results": results})
        return

    @route("POST", "/api/metadata/steam")
    def _api_post_api_metadata_steam(self, payload):
        self.steam_metadata(payload)

    @route("POST", "/api/metadata/sync")
    def _api_post_api_metadata_sync(self, payload):
        self.sync_metadata()

    @route("POST", "/api/metadata/apply")
    def _api_post_api_metadata_apply(self, payload):
        self.apply_metadata(payload)

    @route("POST", "/api/metadata/match")
    def _api_post_api_metadata_match(self, payload):
        self.match_metadata(payload)

    @route("POST", "/api/metadata/igdb/apply")
    def _api_post_api_metadata_igdb_apply(self, payload):
        self.apply_igdb_metadata(payload)

    @route("POST", "/api/v2/metadata/matches/preview")
    def _api_post_api_v2_metadata_matches_preview(self, payload):
        if not METADATA_DATABASE.is_file():
            raise Conflict("Download the metadata database first.")
        payload = payload or {}
        game_ids = payload.get("game_ids")
        import_batch_id = payload.get("import_batch_id")
        if game_ids is not None and not isinstance(game_ids, list):
            raise BadRequest("game_ids must be an array or null.")
        if game_ids and import_batch_id:
            raise BadRequest("Provide either game_ids or import_batch_id.")
        has_games = bool(game_ids)
        has_batch = bool(str(import_batch_id or "").strip())
        if has_games == has_batch:
            raise BadRequest("Provide either game_ids or import_batch_id.")
        preview = create_match_preview_record(
            game_ids=game_ids if has_games else None,
            import_batch_id=str(import_batch_id or "").strip() or None,
        )

        def worker(cancel_event):
            checkpoint = None
            operation = get_operation_service().get(preview.get("job_id") or "")
            if operation:
                checkpoint = operation.get("checkpoint")
            return run_match_preview_job(
                preview["preview_id"],
                database_path=METADATA_DATABASE,
                transact_state=transact_state,
                cancel_event=cancel_event,
                checkpoint=checkpoint,
            )

        job = JOB_MANAGER.submit(
            f"metadata-match-preview:{preview['preview_id']}",
            worker,
            replace=True,
            operation_type="metadata.match_preview",
            input_data={"preview_id": preview["preview_id"]},
        )
        preview = load_match_preview(preview["preview_id"], allow_expired=True)
        preview["job_id"] = job["job_id"]
        preview["state"] = "queued"
        save_match_preview(preview)
        state = _match_preview_state_for_job(job["job_id"])
        self.send_json(
            202,
            {
                "preview_id": preview["preview_id"],
                "revision": preview["revision"],
                "job_id": job["job_id"],
                "state": state,
            },
        )

    @route("GET", "/api/v2/metadata/matches/preview")
    def _api_get_api_v2_metadata_matches_preview(self, parsed):
        query = parse_qs(parsed.query)
        preview_id = str(query.get("preview_id", [""])[0]).strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        try:
            preview = load_match_preview(preview_id)
        except PreviewNotFound:
            raise
        except PreviewExpired:
            raise
        doc = match_preview_document(preview)
        doc["state"] = _match_preview_state_for_job(preview.get("job_id"))
        if preview.get("state") == "ready":
            doc["state"] = "ready"
        self.send_json(200, doc)

    @route("GET", "/api/v2/metadata/matches/items")
    def _api_get_api_v2_metadata_matches_items(self, parsed):
        query = parse_qs(parsed.query)
        preview_id = str(query.get("preview_id", [""])[0]).strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        cursor = query.get("cursor", [None])[0]
        limit = _parse_limit(query.get("limit", [None])[0], default=DEFAULT_MATCH_ITEMS_LIMIT, maximum=MAX_MATCH_ITEMS_LIMIT)
        match_class = query.get("class", [None])[0]
        if match_class and match_class not in {"exact_review", "likely", "possible", "unmatched"}:
            raise BadRequest("Invalid class filter.")
        payload = list_match_preview_items(
            preview_id,
            cursor=cursor,
            limit=limit,
            match_class=match_class,
        )
        self.send_json(200, payload)

    @route("GET", "/api/v2/metadata/media-candidates")
    def _api_get_api_v2_metadata_media_candidates(self, parsed):
        """Image candidates for one LaunchBox database id (thumbnail chooser)."""
        query = parse_qs(parsed.query)
        try:
            database_id = int(str(query.get("database_id", [""])[0]).strip() or 0)
        except (TypeError, ValueError):
            database_id = 0
        if database_id <= 0:
            raise BadRequest("database_id is required.")
        if not METADATA_DATABASE.is_file():
            raise Conflict("Download the metadata database first.")
        kind_for_type = {}
        for kind, lbdb_types in MEDIA_TYPE_MAP.items():
            for lbdb_type in lbdb_types:
                kind_for_type.setdefault(lbdb_type, kind)
        candidates = _lbdb_media_candidates(database_id, kind_for_type)
        self.send_json(200, {"database_id": database_id, "candidates": candidates})

    @route("POST", "/api/v2/metadata/matches/decisions")
    def _api_post_api_v2_metadata_matches_decisions(self, payload):
        payload = payload or {}
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise BadRequest("items must be a non-empty array.")
        result, never_rejections = apply_match_decisions(preview_id, items)
        persist_never_rejections(transact_state, never_rejections)
        self.send_json(200, {"preview_id": preview_id, **result})

    @route("POST", "/api/v2/metadata/matches/apply")
    def _api_post_api_v2_metadata_matches_apply(self, payload):
        if not METADATA_DATABASE.is_file():
            raise Conflict("Download the metadata database first.")
        payload = payload or {}
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        revision = payload.get("revision")
        if revision is None:
            raise BadRequest("revision is required.")
        game_ids = payload.get("game_ids")
        if game_ids is not None and not isinstance(game_ids, list):
            raise BadRequest("game_ids must be an array or null.")
        field_allow_list = payload.get("field_allow_list")
        media_allow_list = payload.get("media_allow_list")
        replace_existing = bool(payload.get("replace_existing"))
        if replace_existing and not field_allow_list and not media_allow_list:
            raise BadRequest("replace_existing requires a non-empty allow-list.")
        try:
            load_match_preview(preview_id)
        except PreviewNotFound:
            raise
        except PreviewExpired:
            raise

        def worker(cancel_event):
            return apply_match_preview(
                preview_id,
                revision=int(revision),
                game_ids=game_ids,
                field_allow_list=field_allow_list,
                media_allow_list=media_allow_list,
                replace_existing=replace_existing,
                database_path=METADATA_DATABASE,
                data_dir=DATA,
                transact_state=transact_state,
                create_backup=create_backup,
                data_parent=DATA.parent,
                running_map=RUNNING,
                cancel_event=cancel_event,
            )

        job = JOB_MANAGER.submit(
            f"metadata-match-apply:{preview_id}",
            worker,
            replace=True,
            operation_type="metadata.apply",
            input_data={"preview_id": preview_id, "revision": int(revision)},
        )
        self.send_json(
            202,
            {
                "job_id": job["job_id"],
                "preview_id": preview_id,
                "revision": int(revision),
            },
        )

    def steam_metadata(self, payload):
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        update_steam_metadata(target)
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id"), **payload})
            game.update(target)
        transact_state(mutate)
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

        JOB_MANAGER.submit("metadata", worker)
        self.send_json(202, {"state":"downloading"})

    def match_metadata(self, payload):
        """Auto-match every unmatched game by exact title to the LBDB."""
        if not METADATA_DATABASE.is_file():
            raise Conflict("Download the metadata database first.")
        platform = str(payload.get("platform", "all"))
        with PROCESS_LOCK:
            if METADATA_JOB.get("state") == "running":
                self.send_json(200, METADATA_JOB)
                return
            METADATA_JOB.clear()
            METADATA_JOB.update({"state":"running", "matched":0, "scanned":0, "errors":[]})

        def worker():
            state = load_state()
            candidates = [
                game for game in state["games"]
                if not game.get("launchbox_db_id")
                and (platform == "all" or game.get("platform") == platform)
                and str(game.get("name") or "").strip()
            ]
            if not candidates:
                with PROCESS_LOCK:
                    METADATA_JOB.update({"state":"done", "scanned":0, "matched":0})
                return
            titles = [(str(game["name"]).strip(), str(game.get("platform") or "")) for game in candidates]
            matches = batch_match(METADATA_DATABASE, titles)
            matched_records = {}
            for game in candidates:
                stable_id = str(game.get("game_id"))
                key = (str(game["name"]).strip(), str(game.get("platform") or ""))
                record = matches.get(key)
                if record:
                    matched_records[stable_id] = (str(record["database_id"]), game.get("name", stable_id))

            matched = 0
            errors = []
            if matched_records:
                def mutate(state):
                    nonlocal matched
                    for stable_id, (db_id, name) in matched_records.items():
                        try:
                            game_from_payload(state, {"game_id": stable_id})["launchbox_db_id"] = db_id
                            matched += 1
                        except (IndexError, KeyError, ValueError) as error:
                            errors.append(f"{name}: {error}")
                try:
                    transact_state(mutate)
                except Exception as error:
                    errors.append(f"Batch state update error: {error}")
            with PROCESS_LOCK:
                METADATA_JOB.update({"state":"done", "scanned":len(candidates), "matched":matched, "errors":errors[-20:]})

        JOB_MANAGER.submit("metadata-match", worker)
        self.send_json(202, {"state":"running"})

    def apply_metadata(self, payload):
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not set(media_types) <= MEDIA_TYPES_ALL:
            raise ValueError("Invalid media selection.")
        # Exact-candidate apply from the thumbnail chooser: these kinds skip
        # the top-pick and download the chosen URL instead. URLs are only
        # accepted when they match a candidate the database carries for the
        # chosen record (trust boundary — no arbitrary client URLs).
        database_id = int(payload["database_id"])
        exact_urls = _clean_exact_media_urls(
            payload.get("media_urls"), _lbdb_candidate_urls(database_id)
        ) if payload.get("media_urls") else {}
        media_types = [kind for kind in media_types if kind not in exact_urls]
        state = load_state()
        original_game = game_from_payload(state, payload)
        if "manual" in media_types and not str(original_game.get("path") or "").strip():
            raise ValueError("This game has no file path, so no manual can be imported.")
        stable_game_id = original_game.get("game_id")
        original = dict(original_game)
        updated = apply_game_metadata(
            dict(original), METADATA_DATABASE, database_id, media_types,
            DATA.parent / "media/launchbox", bool(payload.get("overwrite")),
            region_priority=load_state().get("settings", {}).get("region_priority"),
        )
        if exact_urls:
            media_root = DATA.parent / "media/launchbox" / str(database_id)
            for kind, url in exact_urls.items():
                if not bool(payload.get("overwrite")) and original.get(kind):
                    continue
                try:
                    path = download_bytes(url, media_root / f"{kind}{_auto_scrape_ext(url)}")
                except (OSError, ValueError):
                    continue
                updated[kind] = [path] if kind == "screenshots" else path
        notes = list(updated.pop("_media_notes") or []) if "_media_notes" in updated else []
        changes = {key:value for key,value in updated.items() if original.get(key) != value}
        def mutate(state):
            game_from_payload(state, {"game_id": stable_game_id}).update(changes)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"updated":sorted(changes), "notes":notes})

    def apply_igdb_metadata(self, payload):
        igdb_id = int(payload["igdb_id"])
        state = load_state()
        original = copy.deepcopy(game_from_payload(state, payload))
        stable_game_id = str(original.get("game_id") or "")
        metadata = fetch_igdb_game(igdb_id)
        def mutate(state):
            game = game_from_payload(state, {"game_id": stable_game_id})
            apply_igdb_metadata(game, metadata)
            return game.get("name", "")
        _, name = transact_state(mutate)
        self.send_json(200, {"applied": True, "game": name})


_EXACT_MEDIA_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


# ── Automatic post-import scrape (Flagship 2, ADR 0056) ────────────────────
# queue_auto_scrape() submits exactly two jobs per import batch:
#   metadata-match:{batch}  (operation_type metadata.match_auto)
#   metadata-media:{batch}  (operation_type metadata.media_auto)
# Both surface in Activity/SSE through the standard operation machinery.
# Default is OFFLINE: an online provider runs only when its opt-in setting
# is true AND the provider is configured, with per-run budgets. A single
# game's failure never aborts a pass.

AUTO_SCRAPE_SS_BUDGET = 50
AUTO_SCRAPE_IGDB_BUDGET = 50
AUTO_SCRAPE_SGDB_BUDGET = 50
AUTO_SCRAPE_MATCH_WAIT_SECONDS = 600
AUTO_SCRAPE_MATCH_POLL_SECONDS = 5
AUTO_SCRAPE_SGDB_KINDS = ("cover", "background", "clear_logo", "icon", "banner")
AUTO_SCRAPE_DEFAULT_MEDIA = ("cover", "background", "screenshots")


def _auto_scrape_settings():
    settings = load_state().get("settings", {})
    return {
        "scrape_after_import": bool(settings.get("scrape_after_import", True)),
        "screenscraper": bool(settings.get("scrape_screenscraper_enabled", False)),
        "igdb": bool(settings.get("scrape_igdb_enabled", False)),
        "steamgrid": bool(settings.get("scrape_steamgrid_enabled", False)),
    }


def _igdb_configured():
    try:
        igdb_credentials()
    except ValueError:
        return False
    return True


def _normalize_scrape_title(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _auto_scrape_unmatched(state, import_batch_id):
    """Batch games with no confident provider id yet."""
    return [
        game for game in state.get("games", [])
        if str(game.get("import_batch_id") or "") == import_batch_id
        and not game.get("launchbox_db_id")
        and not game.get("igdb_id")
        and not game.get("screenscraper_id")
    ]


def _auto_scrape_screenscraper_pass(import_batch_id, cancel_event, summary):
    if not _auto_scrape_settings()["screenscraper"] or not screenscraper_configured():
        summary["screenscraper"] = {"skipped": True}
        return
    games = [
        game for game in _auto_scrape_unmatched(load_state(), import_batch_id)
        if Path(str(game.get("path") or "")).is_file()
    ][:AUTO_SCRAPE_SS_BUDGET]
    matched, errors = 0, []
    for game in games:
        if cancel_event is not None and cancel_event.is_set():
            break
        name = str(game.get("name") or "")
        try:
            metadata, tier = confident_hash_match(
                str(game.get("path")),
                system_id=system_id_for_platform(game.get("platform")),
                cache_dir=DATA.parent / "cache",
            )
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")
            continue
        if tier != HASH_TIER_DUAL:
            continue
        stable_id = str(game.get("game_id") or "")
        def mutate(state, stable_id=stable_id, metadata=metadata):
            target = game_from_payload(state, {"game_id": stable_id})
            apply_screenscraper_metadata(target, metadata)
            target["screenscraper_id"] = metadata.get("id")
            target["matched_by"] = "hash"
            target["match_confidence"] = HASH_TIER_DUAL
        try:
            transact_state(mutate)
            matched += 1
        except Exception as error:
            errors.append(f"{name}: {error}")
    summary["screenscraper"] = {"matched": matched, "errors": errors[-10:]}


def _auto_scrape_igdb_pass(import_batch_id, cancel_event, summary):
    if not _auto_scrape_settings()["igdb"] or not _igdb_configured():
        summary["igdb"] = {"skipped": True}
        return
    games = _auto_scrape_unmatched(load_state(), import_batch_id)[:AUTO_SCRAPE_IGDB_BUDGET]
    matched, errors = 0, []
    for game in games:
        if cancel_event is not None and cancel_event.is_set():
            break
        name = str(game.get("name") or "").strip()
        if not name:
            continue
        try:
            candidates = search_igdb_games(name, platform=str(game.get("platform") or ""), limit=5)
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")
            continue
        wanted = _normalize_scrape_title(name)
        top = next((item for item in candidates if _normalize_scrape_title(item.get("name")) == wanted), None)
        if top is None:
            continue
        game_year = str(game.get("year") or "").strip()
        top_year = str(top.get("year") or "").strip()
        if game_year and top_year and game_year != top_year:
            continue
        try:
            metadata = fetch_igdb_game(top["id"])
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")
            continue
        stable_id = str(game.get("game_id") or "")
        def mutate(state, stable_id=stable_id, metadata=metadata):
            target = game_from_payload(state, {"game_id": stable_id})
            apply_igdb_metadata(target, metadata)
            target["matched_by"] = "igdb"
            target["match_confidence"] = "exact_title"
        try:
            transact_state(mutate)
            matched += 1
        except Exception as error:
            errors.append(f"{name}: {error}")
    summary["igdb"] = {"matched": matched, "errors": errors[-10:]}


def _auto_scrape_match_worker(cancel_event, import_batch_id, preview_id):
    summary = {"batch": import_batch_id, "preview_id": preview_id}
    if not METADATA_DATABASE.is_file() or not preview_id:
        summary["lbdb"] = {"skipped": "metadata database not downloaded"}
    else:
        run_match_preview_job(
            preview_id,
            database_path=METADATA_DATABASE,
            transact_state=transact_state,
            cancel_event=cancel_event,
        )
        preview = load_match_preview(preview_id, allow_expired=True)
        summary["lbdb"] = {
            "counts": dict(preview.get("counts") or {}),
            "auto_applied": list(preview.get("auto_applied") or []),
        }
    _auto_scrape_screenscraper_pass(import_batch_id, cancel_event, summary)
    _auto_scrape_igdb_pass(import_batch_id, cancel_event, summary)
    return summary


def _wait_for_match_job(import_batch_id, cancel_event):
    deadline = time.monotonic() + AUTO_SCRAPE_MATCH_WAIT_SECONDS
    name = f"metadata-match:{import_batch_id}"
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        state = str((JOB_MANAGER.snapshot(name) or {}).get("state") or "")
        if state and state not in {"queued", "running", "cancelling"}:
            return state
        time.sleep(AUTO_SCRAPE_MATCH_POLL_SECONDS)
    return "timeout"


def _auto_scrape_ext(url):
    candidate = Path(str(url).split("?")[0]).suffix.casefold()
    return candidate if candidate else ".jpg"


def _auto_scrape_steamgrid_fill(import_batch_id, media_types, cancel_event):
    """Fill still-missing artwork kinds from SteamGridDB for matched games."""
    if not _auto_scrape_settings()["steamgrid"] or not steamgrid_configured():
        return {"skipped": True}
    kinds = [kind for kind in AUTO_SCRAPE_SGDB_KINDS if kind in media_types]
    if not kinds:
        return {"skipped": "no steamgrid kinds requested"}
    state = load_state()
    targets = [
        game for game in state.get("games", [])
        if str(game.get("import_batch_id") or "") == import_batch_id
        and (game.get("launchbox_db_id") or game.get("igdb_id") or game.get("screenscraper_id"))
        and any(not game.get(kind) for kind in kinds)
    ][:AUTO_SCRAPE_SGDB_BUDGET]
    filled, errors = 0, []
    for game in targets:
        if cancel_event is not None and cancel_event.is_set():
            break
        name = str(game.get("name") or "")
        missing = [kind for kind in kinds if not game.get(kind)]
        try:
            found = search_steamgrid_games(name, limit=1, cache_dir=DATA.parent / "cache")
            if not found:
                continue
            metadata = steamgrid_game_info(found[0].get("id"), cache_dir=DATA.parent / "cache")
            urls = choose_steamgrid_media(metadata, missing)
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")
            continue
        slug = "".join(char if char.isalnum() or char in "-_ " else "" for char in name).strip().replace(" ", "-")[:60]
        media_root = DATA.parent / "media" / "steamgrid" / (slug or str(game.get("game_id")))
        downloaded = {}
        for kind, url in urls.items():
            url = clean_steamgrid_url(url)
            if not url:
                continue
            try:
                downloaded[kind] = download_bytes(url, media_root / f"{kind}{_auto_scrape_ext(url)}")
            except (OSError, ValueError):
                continue
        if not downloaded:
            continue
        stable_id = str(game.get("game_id") or "")
        def mutate(state, stable_id=stable_id, downloaded=downloaded):
            game_from_payload(state, {"game_id": stable_id}).update(downloaded)
        try:
            transact_state(mutate)
            filled += 1
        except Exception as error:
            errors.append(f"{name}: {error}")
    if filled:
        bump_media_epoch()
    return {"filled": filled, "errors": errors[-10:]}


def _auto_scrape_media_worker(cancel_event, import_batch_id, media_types, overwrite):
    summary = {"batch": import_batch_id, "media_types": list(media_types)}
    summary["match_wait"] = _wait_for_match_job(import_batch_id, cancel_event)
    state = load_state()
    targets = [
        game for game in state.get("games", [])
        if str(game.get("import_batch_id") or "") == import_batch_id and game.get("launchbox_db_id")
    ]
    summary["targets"] = len(targets)
    if not METADATA_DATABASE.is_file():
        summary["lbdb"] = {"skipped": "metadata database not downloaded"}
    else:
        changes_by_id, errors = {}, []
        for game in targets:
            if cancel_event is not None and cancel_event.is_set():
                break
            stable_id = str(game.get("game_id") or "")
            original = dict(game)
            try:
                updated = apply_game_metadata(
                    dict(original), METADATA_DATABASE, int(game["launchbox_db_id"]),
                    list(media_types), DATA.parent / "media/launchbox", overwrite,
                )
                updated.pop("_media_notes", None)
                changes = {key: value for key, value in updated.items() if original.get(key) != value}
                if changes:
                    changes_by_id[stable_id] = changes
            except Exception as error:
                errors.append(f"{original.get('name', stable_id)}: {error}")
        if changes_by_id:
            def mutate(state, changes_by_id=changes_by_id):
                for stable_id, changes in changes_by_id.items():
                    game_from_payload(state, {"game_id": stable_id}).update(changes)
            transact_state(mutate)
            bump_media_epoch()
        summary["lbdb"] = {"updated": len(changes_by_id), "errors": errors[-10:]}
    # Optional SteamGridDB fill runs regardless of LBDB availability.
    summary["steamgrid"] = _auto_scrape_steamgrid_fill(import_batch_id, media_types, cancel_event)
    return summary


def queue_auto_scrape(import_batch_id, *, media_types=None, overwrite=False):
    """Submit exactly the two auto-scrape jobs for one import batch.

    Returns (match_job_id, media_job_id, preview_id). preview_id is None
    when the metadata database is not downloaded; the match job then only
    runs the opted-in online passes.
    """
    batch = str(import_batch_id or "").strip()
    if not batch:
        raise ValueError("import_batch_id is required for auto-scrape.")
    if media_types is None:
        settings = load_state().get("settings", {})
        media_types = sorted(set(settings.get("auto_import_media_types") or AUTO_SCRAPE_DEFAULT_MEDIA))
    media_types = [str(kind) for kind in media_types]
    if not set(media_types) <= set(MEDIA_TYPES_ALL):
        raise ValueError("Invalid media type selection.")
    preview_id = None
    if METADATA_DATABASE.is_file():
        preview_id = create_match_preview_record(import_batch_id=batch)["preview_id"]

    def match_worker(cancel_event, batch=batch, preview_id=preview_id):
        return _auto_scrape_match_worker(cancel_event, batch, preview_id)

    def media_worker(cancel_event, batch=batch, media_types=media_types, overwrite=overwrite):
        return _auto_scrape_media_worker(cancel_event, batch, media_types, overwrite)

    match_job = JOB_MANAGER.submit(
        f"metadata-match:{batch}",
        match_worker,
        replace=True,
        operation_type="metadata.match_auto",
        input_data={"import_batch_id": batch, "preview_id": preview_id},
    )
    media_job = JOB_MANAGER.submit(
        f"metadata-media:{batch}",
        media_worker,
        replace=True,
        operation_type="metadata.media_auto",
        input_data={"import_batch_id": batch, "media_types": media_types},
    )
    return match_job["job_id"], media_job["job_id"], preview_id


@route("POST", "/api/v2/metadata/auto-scrape", spec="handlers.metadata.metadata_auto_scrape")
def metadata_auto_scrape(handler, payload):
    """Queue the two auto-scrape jobs for one import batch. 202 on success; v2 only."""
    payload = payload or {}
    batch = str(payload.get("import_batch_id") or "").strip()
    if not batch:
        raise BadRequest("import_batch_id is required.")
    if not _auto_scrape_settings()["scrape_after_import"]:
        handler.send_json(200, {"queued": False, "reason": "scrape_after_import is disabled"})
        return
    try:
        match_job_id, media_job_id, preview_id = queue_auto_scrape(
            batch,
            media_types=payload.get("media_types"),
            overwrite=bool(payload.get("overwrite")),
        )
    except ValueError as error:
        raise BadRequest(str(error)) from None
    handler.send_json(202, {
        "queued": True,
        "import_batch_id": batch,
        "match_job_id": match_job_id,
        "media_job_id": media_job_id,
        "preview_id": preview_id,
    })


SCRAPE_SETTING_KEYS = (
    "scrape_after_import",
    "scrape_screenscraper_enabled",
    "scrape_igdb_enabled",
    "scrape_steamgrid_enabled",
)
SCRAPE_SETTING_DEFAULTS = {
    "scrape_after_import": True,
    "scrape_screenscraper_enabled": False,
    "scrape_igdb_enabled": False,
    "scrape_steamgrid_enabled": False,
}


@route("GET", "/api/v2/metadata/scrape-settings", spec="handlers.metadata._api_get_api_v2_metadata_scrape_settings")
def _api_get_api_v2_metadata_scrape_settings(handler, parsed):
    """Read the auto-scrape master toggle + provider opt-ins (owned persistence).

    These live in raw state settings so the feature never touches the
    settings-handler normalization or the public-settings projection.
    """
    settings = load_state().get("settings", {})
    handler.send_json(200, {
        key: bool(settings.get(key, SCRAPE_SETTING_DEFAULTS[key]))
        for key in SCRAPE_SETTING_KEYS
    })


@route("POST", "/api/v2/metadata/scrape-settings", spec="handlers.metadata._api_post_api_v2_metadata_scrape_settings")
def _api_post_api_v2_metadata_scrape_settings(handler, payload):
    """Persist the auto-scrape master toggle + provider opt-ins (owned persistence).

    Only the four known keys are written, bool-coerced; anything else in the
    payload is ignored.
    """
    payload = payload or {}
    updates = {
        key: bool(payload.get(key, SCRAPE_SETTING_DEFAULTS[key]))
        for key in SCRAPE_SETTING_KEYS
        if key in payload
    }
    def mutate(state):
        settings = state.setdefault("settings", {})
        settings.update(updates)
    transact_state(mutate)
    settings = load_state().get("settings", {})
    handler.send_json(200, {
        key: bool(settings.get(key, SCRAPE_SETTING_DEFAULTS[key]))
        for key in SCRAPE_SETTING_KEYS
    })


def _lbdb_media_candidates(database_id, kind_for_type):
    """Image candidates for one LaunchBox database id, as ``{kind, type, url, region}`` dicts."""
    connection = sqlite3.connect(METADATA_DATABASE)
    try:
        rows = connection.execute(
            "SELECT type, filename, region FROM images WHERE database_id = ? ORDER BY type, region",
            (database_id,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "kind": kind_for_type[str(image_type or "")],
            "type": str(image_type or ""),
            "url": IMAGE_URL + str(filename or ""),
            "region": str(region or ""),
        }
        for image_type, filename, region in rows
        if filename and str(image_type or "") in kind_for_type
    ]


def _lbdb_candidate_urls(database_id):
    """``{(kind, url)}`` pairs the server may download for one LaunchBox record.

    Trust boundary for exact-thumbnail apply: a ``media_urls`` value is only
    downloaded when it matches a candidate the database itself carries for the
    chosen record, so a client cannot make the server fetch an arbitrary URL.
    """
    kind_for_type = {}
    for kind, lbdb_types in MEDIA_TYPE_MAP.items():
        for lbdb_type in lbdb_types:
            kind_for_type.setdefault(lbdb_type, kind)
    return {
        (entry["kind"], entry["url"])
        for entry in _lbdb_media_candidates(database_id, kind_for_type)
    }


def _clean_exact_media_urls(value, allowed):
    """Validate an exact-candidate ``{kind: url}`` map for thumbnail chooser apply.

    Only http(s) URLs for known media kinds that appear in ``allowed``
    (the candidate set for the chosen provider record) survive; anything
    else is dropped so the client cannot pick an arbitrary download URL.
    """
    cleaned = {}
    if isinstance(value, dict):
        for kind, url in value.items():
            kind = str(kind or "").strip()
            url = str(url or "").strip()
            if kind in MEDIA_TYPES_ALL and _EXACT_MEDIA_URL_RE.match(url) and (kind, url) in allowed:
                cleaned[kind] = url
    return cleaned
