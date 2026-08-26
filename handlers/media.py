"""MediaHandlers capability handlers. Media serving, audit, bulk download, cleanup, screenshots, OBS, bezels, and EmuMovies."""

import copy
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from api_errors import BadgeNotFound, MediaNotFound
from metadata import apply_game_metadata
from openbox import load_state
from routes.registry import route
from parity_integrations import attach_recording, capture_screenshot, download_bezel, download_emumovies_media, load_emumovies_credentials, obs_recording_status, save_emumovies_credentials
from parity_media import active_video, cleanup_duplicates, find_duplicate_media, load_media_queue
from parity_premium import apply_media_pack, download_gog_media, download_steam_trailer, list_media_packs, platform_categories, strings_for
from webapp_state import DATA, JOB_MANAGER, MEDIA_JOB, MEDIA_TYPES_ALL, METADATA_DATABASE, PROCESS_LOCK, approved_media_path, bump_media_epoch, download_image, game_from_payload, game_from_query, load_state_view, media_probe_path, public_settings, transact_state


class MediaHandlers:
    @route("GET", "/api/media/audit")
    def _api_get_api_media_audit(self, parsed):
        query = parse_qs(parsed.query)
        platform = query.get("platform", ["all"])[0]
        games = [
            game for game in load_state_view()["games"]
            if platform == "all" or game.get("platform") == platform
        ]
        def screenshot_paths(game):
            screenshots = game.get("screenshots", [])
            return screenshots if isinstance(screenshots, list) else []
        self.send_json(200, {
            "games":len(games),
            "matched":sum(bool(game.get("launchbox_db_id")) for game in games),
            "missing_cover":sum(not media_probe_path(game.get("cover")) for game in games),
            "missing_background":sum(not media_probe_path(game.get("background")) for game in games),
            "missing_screenshots":sum(not any(media_probe_path(path) for path in screenshot_paths(game) if path) for game in games),
            "missing_box_back":sum(not media_probe_path(game.get("box_back")) for game in games),
            "missing_box_spine":sum(not media_probe_path(game.get("box_spine")) for game in games),
            "missing_box_3d":sum(not media_probe_path(game.get("box_3d")) for game in games),
            "missing_clear_logo":sum(not media_probe_path(game.get("clear_logo")) for game in games),
            "missing_fanart":sum(not media_probe_path(game.get("fanart")) for game in games),
            "missing_banner":sum(not media_probe_path(game.get("banner")) for game in games),
            "missing_icon":sum(not media_probe_path(game.get("icon")) for game in games),
            "missing_title_screen":sum(not media_probe_path(game.get("title_screen")) for game in games),
            "missing_cart_front":sum(not media_probe_path(game.get("cart_front")) for game in games),
            "missing_cart_back":sum(not media_probe_path(game.get("cart_back")) for game in games),
            "missing_disc":sum(not media_probe_path(game.get("disc")) for game in games),
            "missing_advertisement":sum(not media_probe_path(game.get("advertisement")) for game in games),
            "missing_manual":sum(not media_probe_path(game.get("manual")) for game in games),
        })
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
            elif kind in {"cover", "background", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual", "video", "music", "video_snap", "video_theme", "video_trailer", "video_recording"}:
                if kind == "video":
                    _, video_path = active_video(game)
                    media = Path(video_path or game.get("video", ""))
                else:
                    media = Path(game.get(kind, ""))
            else:
                raise ValueError
            media = approved_media_path(media, must_exist=True)
            self.send_file(200, media)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise MediaNotFound("Media not found") from None
        return

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
            for current, (stable_id, database_id) in enumerate(targets, 1):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if stable_id in completed_set:
                    continue
                original = {}
                try:
                    state = load_state()
                    original = dict(game_from_payload(state, {"game_id": stable_id}))
                    updated = apply_game_metadata(
                        dict(original), METADATA_DATABASE, int(database_id), media_types,
                        DATA.parent / "media/launchbox", overwrite,
                    )
                    notes = updated.pop("_media_notes") if "_media_notes" in updated else None
                    if notes:
                        manual_missing += 1
                    changes = {key: value for key, value in updated.items() if original.get(key) != value}
                    if changes:
                        def mutate(state, stable_id=stable_id, changes=changes):
                            game_from_payload(state, {"game_id": stable_id}).update(changes)
                        transact_state(mutate)
                        updated_count += 1
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
        path = download_emumovies_media(
            target, credentials, DATA.parent / "media", str(payload.get("type", "box")),
        )
        def mutate(state):
            game = game_from_payload(state, {"game_id": target.get("game_id")})
            game.update(target)
            game["cover"] = path
        transact_state(mutate)
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
