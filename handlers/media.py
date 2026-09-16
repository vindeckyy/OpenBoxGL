"""MediaHandlers capability handlers. Media serving, audit, bulk download, cleanup, screenshots, OBS, bezels, and EmuMovies."""

import copy
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadgeNotFound, MediaNotFound, RangeParseError
from metadata import apply_game_metadata
from openbox import load_state
from routes.registry import route
from parity_integrations import attach_recording, capture_screenshot, download_bezel, download_emumovies_media, load_emumovies_credentials, obs_recording_status, save_emumovies_credentials
from parity_media import active_video, cleanup_duplicates, find_duplicate_media, load_media_queue
from parity_memories import IMPORT_JOB_NAME, MEMORIES_FIELD, UNASSIGNED_FIELD, apply_import_plan, collect_imports, configured_roots, memories_import_enabled
from parity_premium import apply_media_pack, download_gog_media, download_steam_trailer, list_media_packs, platform_categories, strings_for
from webapp_state import DATA, JOB_MANAGER, MEDIA_JOB, MEDIA_TYPES_ALL, METADATA_DATABASE, PROCESS_LOCK, approved_media_path, bump_media_epoch, download_image, game_from_payload, game_from_query, load_state_view, public_settings, transact_state


# EmuMovies media type -> library game field.  Unknown types are rejected so a
# download can never silently overwrite ``cover`` with the wrong asset.
EMUMOVIES_TYPE_FIELDS = {
    "box": "cover",
    "box_front": "cover",
    "cover": "cover",
    "boxback": "box_back",
    "box_back": "box_back",
    "boxspine": "box_spine",
    "box_spine": "box_spine",
    "box3d": "box_3d",
    "box_3d": "box_3d",
    "snap": "screenshots",
    "screenshot": "screenshots",
    "screenshots": "screenshots",
    "title": "title_screen",
    "titlescreen": "title_screen",
    "title_screen": "title_screen",
    "marquee": "banner",
    "banner": "banner",
    "cart": "cart_front",
    "cartfront": "cart_front",
    "cart_front": "cart_front",
    "cartback": "cart_back",
    "cart_back": "cart_back",
    "disc": "disc",
    "logo": "clear_logo",
    "clearlogo": "clear_logo",
    "clear_logo": "clear_logo",
    "fanart": "fanart",
    "background": "background",
    "icon": "icon",
    "manual": "manual",
    "advertisement": "advertisement",
}

# Bulk media downloads at or below this many targets keep the historical
# one-transaction-per-game granularity; larger batches commit once.
BULK_MEDIA_BATCH_THRESHOLD = 25


class MediaHandlers:
    @route("GET", "/api/media/audit")
    def _api_get_api_media_audit(self, parsed):
        from pkg.state.media_probe import media_probe_paths_batch
        query = parse_qs(parsed.query)
        platform = query.get("platform", ["all"])[0]
        games = [
            game for game in load_state_view()["games"]
            if platform == "all" or game.get("platform") == platform
        ]
        audit_fields = (
            "cover", "background", "screenshots", "box_back", "box_spine",
            "box_3d", "clear_logo", "fanart", "banner", "icon",
            "title_screen", "cart_front", "cart_back", "disc",
            "advertisement", "manual",
        )
        values = []
        for game in games:
            for field in audit_fields:
                if field == "screenshots":
                    screenshots = game.get("screenshots", [])
                    if isinstance(screenshots, list):
                        values.extend(screenshots)
                else:
                    values.append(game.get(field))
        # One batched stat pass for the whole audit instead of 300k individual
        # probe calls that thrash the file-probe cache (P2-7).
        probed = media_probe_paths_batch(values)
        payload = {
            "games": len(games),
            "matched": sum(bool(game.get("launchbox_db_id")) for game in games),
        }
        for field in audit_fields:
            if field == "screenshots":
                payload["missing_screenshots"] = sum(
                    1 for game in games
                    if not any(
                        probed.get(str(path or ""), False)
                        for path in (game.get("screenshots") if isinstance(game.get("screenshots"), list) else [])
                        if path
                    )
                )
                continue
            payload[f"missing_{field}"] = sum(
                1 for game in games if not probed.get(str(game.get(field) or ""), False)
            )
        self.send_json(200, payload)
        return

    @route("GET", "/api/media/bulk/status")
    def _api_get_api_media_bulk_status(self, parsed):
        with PROCESS_LOCK:
            job = dict(MEDIA_JOB)
        self.send_json(200, {"job":job})
        return

    @route("GET", "/api/ra/badge")
    def _api_get_api_ra_badge(self, parsed):
        query = parse_qs(parsed.query)
        name = re.sub(r"[^A-Za-z0-9_-]", "", query.get("name", [""])[0])
        locked = query.get("locked", ["0"])[0] == "1"
        if not name:
            raise BadgeNotFound("Badge not found")
        try:
            badge = approved_media_path(DATA.parent / "media/retroachievements/badges" / f"{name}{'_lock' if locked else ''}.png")
            if not badge.is_file():
                download_image(f"https://media.retroachievements.org/Badge/{badge.name}", badge)
            self.send_file(200, badge, "image/png")
        except (OSError, ValueError):
            raise BadgeNotFound("Badge not found") from None
        return

    @route("GET", "/api/media")
    def _api_get_api_media(self, parsed):
        query = parse_qs(parsed.query)
        try:
            game = game_from_query(load_state_view(), query)
            kind = query["kind"][0]
            if kind == "screenshot":
                index = int(query["index"][0])
                media = Path(game.get("screenshots", [])[index])
            elif kind == "clip":
                index = int(query["index"][0])
                clips = game.get("clips", [])
                if not isinstance(clips, list):
                    raise ValueError
                media = Path(clips[index].get("path", ""))
            elif kind in {"cover", "background", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual", "video", "music", "video_snap", "video_theme", "video_trailer", "video_recording"}:
                if kind == "video":
                    _, video_path = active_video(game)
                    media = Path(video_path or game.get("video", ""))
                else:
                    media = Path(game.get(kind, ""))
            else:
                raise ValueError
            media = approved_media_path(media, must_exist=True)
            self._serve_media(media)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise MediaNotFound("Media not found") from None
        return

    def _serve_media(self, media):
        """Serve *media*, mapping only a malformed ``Range`` request to 416.

        ``send_file`` signals a bad range spec with ``RangeParseError`` (a
        ``ValueError``). Missing files raise ``OSError`` and stay a 404, and
        unrelated ``ValueError`` failures propagate instead of being answered
        as an unsatisfiable range.
        """
        try:
            self.send_file(200, media)
        except RangeParseError:
            self._send_range_unsatisfiable(media)

    def _send_range_unsatisfiable(self, media):
        """Answer a malformed ``Range`` header with 416 + ``Content-Range``.

        ``send_file`` raises ``ValueError`` for an unparseable range spec;
        the route's ``except ValueError`` would otherwise mask it as 404.
        """
        try:
            size = Path(media).stat().st_size
        except OSError:
            size = 0
        self.send_response(416)
        self.headers_common("application/octet-stream", cache_control="private, max-age=31536000, immutable")
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @route("GET", "/api/media/duplicates")
    def _api_get_api_media_duplicates(self, parsed):
        self.send_json(200, {"groups": find_duplicate_media(load_state_view()["games"])})
        return

    @route("GET", "/api/media/queue")
    def _api_get_api_media_queue(self, parsed):
        self.send_json(200, {"queue": load_media_queue(DATA.parent / "media-queue.json")})
        return

    @route("GET", "/api/obs/status")
    def _api_get_api_obs_status(self, parsed):
        self.send_json(200, obs_recording_status())
        return

    @route("GET", "/api/premium/strings")
    def _api_get_api_premium_strings(self, parsed):
        locale = parse_qs(parsed.query).get("locale", ["en"])[0]
        self.send_json(200, {"locale": locale, "strings": strings_for(locale)})
        return

    @route("GET", "/api/premium/media-packs")
    def _api_get_api_premium_media_packs(self, parsed):
        self.send_json(200, {"packs": list_media_packs(load_state_view().get("settings", {}))})
        return

    @route("GET", "/api/premium/platform-categories")
    def _api_get_api_premium_platform_categories(self, parsed):
        self.send_json(200, {"categories": platform_categories(load_state_view().get("settings", {}))})
        return

    @route("POST", "/api/premium/media-packs/apply")
    def _api_post_api_premium_media_packs_apply(self, payload):
        self.apply_media_pack_route(payload)

    @route("POST", "/api/metadata/trailer")
    def _api_post_api_metadata_trailer(self, payload):
        self.download_trailer(payload)

    @route("POST", "/api/metadata/gog")
    def _api_post_api_metadata_gog(self, payload):
        self.download_gog_route(payload)

    @route("POST", "/api/media/bulk")
    def _api_post_api_media_bulk(self, payload):
        self.bulk_media(payload)

    @route("POST", "/api/bezels/download")
    def _api_post_api_bezels_download(self, payload):
        self.download_bezels(payload)

    @route("POST", "/api/emumovies/settings")
    def _api_post_api_emumovies_settings(self, payload):
        self.save_emumovies(payload)

    @route("POST", "/api/emumovies/download")
    def _api_post_api_emumovies_download(self, payload):
        self.emumovies_download(payload)

    @route("POST", "/api/media/cleanup")
    def _api_post_api_media_cleanup(self, payload):
        self.cleanup_media(payload)

    @route("POST", "/api/screenshot")
    def _api_post_api_screenshot(self, payload):
        self.take_screenshot(payload)

    @route("POST", "/api/obs/attach")
    def _api_post_api_obs_attach(self, payload):
        self.obs_attach(payload)

    def bulk_media(self, payload):
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not media_types or not set(media_types) <= MEDIA_TYPES_ALL:
            raise ValueError("Select at least one valid media type.")
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        platform = str(payload.get("platform", "all"))
        overwrite = bool(payload.get("overwrite"))
        retry_failed = bool(payload.get("retry_failed"))
        explicit_game_ids = payload.get("game_ids")
        if explicit_game_ids is not None and not isinstance(explicit_game_ids, list):
            raise ValueError("game_ids must be an array.")
        with PROCESS_LOCK:
            if MEDIA_JOB.get("state") == "running":
                self.send_json(200, MEDIA_JOB)
                return
            completed_ids = list(MEDIA_JOB.get("completed_game_ids") or []) if retry_failed else []
            failed_ids = list(MEDIA_JOB.get("failed_game_ids") or []) if retry_failed else []
            MEDIA_JOB.clear()
            MEDIA_JOB.update({
                "state": "running",
                "current": 0,
                "total": 0,
                "updated": 0,
                "errors": [],
                "completed_game_ids": completed_ids,
                "failed_game_ids": failed_ids,
            })

        def worker(cancel_event=None):
            state = load_state()
            games_by_id = {
                str(game.get("game_id")): game
                for game in state["games"]
                if game.get("game_id")
            }
            targets = [
                (str(game.get("game_id")), str(game.get("launchbox_db_id")))
                for game in state["games"]
                if game.get("launchbox_db_id") and (platform == "all" or game.get("platform") == platform)
            ]
            if retry_failed:
                wanted = set(str(game_id) for game_id in failed_ids)
                targets = [target for target in targets if target[0] in wanted]
            elif explicit_game_ids:
                wanted = {str(game_id) for game_id in explicit_game_ids}
                targets = [target for target in targets if target[0] in wanted]
            completed_set = set(MEDIA_JOB.get("completed_game_ids") or [])
            failed_set = set(MEDIA_JOB.get("failed_game_ids") or [])
            with PROCESS_LOCK:
                MEDIA_JOB["total"] = len(targets)
            updated_count = 0
            errors = []
            manual_missing = 0
            # Large batches accumulate changes and commit once; small batches
            # keep the historical per-game transaction granularity (P2-6).
            changes_by_id = {}
            names_by_id = {}
            for current, (stable_id, database_id) in enumerate(targets, 1):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if stable_id in completed_set:
                    continue
                original = {}
                try:
                    original = dict(games_by_id.get(stable_id) or {})
                    names_by_id[stable_id] = str(original.get("name") or stable_id)
                    updated = apply_game_metadata(
                        dict(original), METADATA_DATABASE, int(database_id), media_types,
                        DATA.parent / "media/launchbox", overwrite,
                    )
                    notes = updated.pop("_media_notes") if "_media_notes" in updated else None
                    if notes:
                        manual_missing += 1
                    changes = {key: value for key, value in updated.items() if original.get(key) != value}
                    if changes:
                        changes_by_id[stable_id] = changes
                    else:
                        completed_set.add(stable_id)
                    failed_set.discard(stable_id)
                except (OSError, ValueError, sqlite3.Error, Exception) as error:
                    errors.append(f"{original.get('name', stable_id)}: {error}")
                    failed_set.add(stable_id)
                with PROCESS_LOCK:
                    MEDIA_JOB.update({
                        "current": current,
                        "updated": updated_count,
                        "errors": errors[-20:],
                        "manual_missing": manual_missing,
                        "completed_game_ids": sorted(completed_set),
                        "failed_game_ids": sorted(failed_set),
                    })

            def mutate(state, changes_by_id=changes_by_id):
                for stable_id, changes in changes_by_id.items():
                    game_from_payload(state, {"game_id": stable_id}).update(changes)

            pending = list(changes_by_id)
            if pending and len(targets) <= BULK_MEDIA_BATCH_THRESHOLD:
                for stable_id in pending:
                    try:
                        transact_state(
                            lambda state, stable_id=stable_id, changes=changes_by_id[stable_id]:
                            game_from_payload(state, {"game_id": stable_id}).update(changes)
                        )
                        updated_count += 1
                        completed_set.add(stable_id)
                    except (OSError, ValueError, sqlite3.Error, Exception) as error:
                        errors.append(f"{names_by_id.get(stable_id, stable_id)}: {error}")
                        failed_set.add(stable_id)
            elif pending:
                try:
                    transact_state(mutate)
                    updated_count += len(pending)
                    completed_set.update(pending)
                except (OSError, ValueError, sqlite3.Error, Exception) as error:
                    errors.append(f"batch state update failed: {error}")
                    failed_set.update(pending)
            if pending:
                with PROCESS_LOCK:
                    MEDIA_JOB.update({
                        "updated": updated_count,
                        "errors": errors[-20:],
                        "completed_game_ids": sorted(completed_set),
                        "failed_game_ids": sorted(failed_set),
                    })

            bump_media_epoch()
            final_state = "done"
            if failed_set and completed_set:
                final_state = "partial"
            elif failed_set and not completed_set:
                final_state = "error"
            with PROCESS_LOCK:
                MEDIA_JOB.update({"state": final_state, "errors": errors[-20:]})

        JOB_MANAGER.submit("media-bulk", worker)
        self.send_json(202, {"state": "running"})

    def apply_media_pack_route(self, payload):
        pack_id = str(payload.get("id", "")).strip()
        def mutate(state):
            return apply_media_pack(state, pack_id)
        state, pack = transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"pack": pack, "settings": public_settings(state)})

    def download_trailer(self, payload):
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        path = download_steam_trailer(target, DATA.parent / "media")
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"video_trailer": path})

    def download_gog_route(self, payload):
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        download_gog_media(target, DATA.parent / "media")
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"cover": target.get("cover", ""), "background": target.get("background", "")})

    def download_bezels(self, payload):
        platform = str(payload.get("platform", "")).strip()
        path = download_bezel(platform, DATA.parent / "bezels")
        self.send_json(200, {"path": path})

    def save_emumovies(self, payload):
        save_emumovies_credentials(
            DATA.parent,
            str(payload.get("username", "")),
            str(payload.get("password", "")),
        )
        self.send_json(200, {"configured": True})

    def emumovies_download(self, payload):
        credentials = load_emumovies_credentials(DATA.parent)
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        media_type = str(payload.get("type", "box")).strip() or "box"
        field = EMUMOVIES_TYPE_FIELDS.get(media_type.casefold())
        if field is None:
            raise ValueError(f"Unsupported EmuMovies media type: {media_type}")
        path = download_emumovies_media(
            target, credentials, DATA.parent / "media", media_type,
        )
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
            if field == "screenshots":
                screenshots = game.get("screenshots")
                if not isinstance(screenshots, list):
                    screenshots = []
                    game["screenshots"] = screenshots
                if path not in screenshots:
                    screenshots.append(path)
            else:
                game[field] = path
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"path": path})

    def cleanup_media(self, payload):
        platform = str(payload.get("platform", "")).strip()
        state = load_state()
        games = state.get("games", [])
        if platform and platform != "all":
            games = [g for g in games if str(g.get("platform", "")).strip() == platform]
        groups = find_duplicate_media(games, allowed_roots=[DATA.parent])
        apply = bool(payload.get("apply"))
        deleted = cleanup_duplicates(groups, dry_run=not apply, allowed_roots=[DATA.parent])
        if apply and deleted:
            bump_media_epoch()
        self.send_json(200, {"groups": len(groups), "paths": deleted, "applied": apply, "platform": platform or "all"})

    def take_screenshot(self, payload):
        state = load_state()
        game = game_from_payload(state, payload)
        stable_game_id = game.get("game_id")
        destination = DATA.parent / "media" / "captures" / f"{Path(game.get('path', 'game')).stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        path = capture_screenshot(destination)
        def mutate(state):
            screenshots = game_from_payload(state, {"game_id": stable_game_id}).setdefault("screenshots", [])
            if path not in screenshots:
                screenshots.append(path)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"path": path})

    def obs_attach(self, payload):
        video_path = str(payload.get("path", "")).strip()
        state = load_state()
        target = copy.deepcopy(game_from_payload(state, payload))
        path = attach_recording(target, video_path)
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
        transact_state(mutate)
        bump_media_epoch()
        self.send_json(200, {"path": path, "obs": obs_recording_status()})

    # --- S5: memory roots auto-import -------------------------------------
    # Additive v2 surface. All filesystem work happens in the background job;
    # the status/list routes read state only and never scan roots themselves.

    @route("GET", "/api/v2/memories/status")
    def _api_get_api_v2_memories_status(self, parsed):
        state = load_state_view()
        settings = state.get("settings") or {}
        memories_count = 0
        games_with = 0
        for game in state.get("games") or []:
            entries = game.get(MEMORIES_FIELD)
            if isinstance(entries, list) and entries:
                games_with += 1
                memories_count += len(entries)
        unassigned = state.get(UNASSIGNED_FIELD)
        self.send_json(200, {
            "enabled": memories_import_enabled(settings),
            "roots": len(configured_roots(settings)),
            "memories": memories_count,
            "games_with_memories": games_with,
            "unassigned": len(unassigned) if isinstance(unassigned, list) else 0,
            "job": JOB_MANAGER.snapshot(IMPORT_JOB_NAME),
        })

    @route("GET", "/api/v2/memories")
    def _api_get_api_v2_memories(self, parsed):
        query = parse_qs(parsed.query)
        state = load_state_view()
        if query.get("unassigned", [""])[0] in {"1", "true", "yes"}:
            entries = state.get(UNASSIGNED_FIELD)
            source = entries if isinstance(entries, list) else []
        else:
            game = game_from_query(state, query)
            entries = game.get(MEMORIES_FIELD)
            source = entries if isinstance(entries, list) else []
        self.send_json(200, {"memories": self._memories_public(source)})

    @route("GET", "/api/v2/memories/media")
    def _api_get_api_v2_memories_media(self, parsed):
        query = parse_qs(parsed.query)
        state = load_state_view()
        try:
            if query.get("bucket", [""])[0] == "unassigned":
                entries = state.get(UNASSIGNED_FIELD)
                source = entries if isinstance(entries, list) else []
            else:
                source = game_from_query(state, query).get(MEMORIES_FIELD) or []
            index = int(query["index"][0])
            entry = source[index]
            media = approved_media_path(entry.get("path"), must_exist=True)
            self._serve_media(media)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError, FileNotFoundError):
            raise MediaNotFound("Media not found") from None
        return

    @route("POST", "/api/v2/memories/import")
    def _api_post_api_v2_memories_import(self, payload):
        settings = (load_state().get("settings") or {})
        if not memories_import_enabled(settings):
            self.send_json(200, {"enabled": False, "state": "disabled", "added": 0})
            return

        def worker(cancel_event=None):
            state = load_state()
            current_settings = state.get("settings") or {}
            if not memories_import_enabled(current_settings):
                return {"enabled": False, "added": 0}
            plan = collect_imports(
                state, DATA.parent,
                extra_roots=configured_roots(current_settings),
                progress=cancel_event.progress if cancel_event is not None else None,
                cancel=cancel_event,
            )
            _, applied = transact_state(lambda s: apply_import_plan(s, plan))
            counts = dict(plan["counts"])
            counts["added"] = applied["attached"]
            counts["unassigned"] = applied["unassigned"]
            counts["failed"] += applied["failed"]
            if applied["attached"] or applied["unassigned"]:
                bump_media_epoch()
            return counts

        job = JOB_MANAGER.submit(IMPORT_JOB_NAME, worker)
        self.send_json(202, {"state": job.get("state", "queued"), "job_id": job.get("job_id", "")})

    @staticmethod
    def _memories_public(entries):
        """Project stored memory entries to the public shape, dropping
        anything whose path no longer resolves under the media root."""
        public = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                resolved = approved_media_path(entry.get("path"), must_exist=True)
            except (OSError, ValueError):
                continue
            public.append({
                "path": str(resolved),
                "sha256": str(entry.get("sha256") or ""),
                "bytes": int(entry.get("bytes") or 0),
                "source": str(entry.get("source") or ""),
                "taken_at": str(entry.get("taken_at") or ""),
                "imported_at": str(entry.get("imported_at") or ""),
                "hint": str(entry.get("hint") or ""),
            })
        return public
