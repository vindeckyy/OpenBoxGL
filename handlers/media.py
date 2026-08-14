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
from parity_integrations import attach_recording, capture_screenshot, download_bezel, download_emumovies_media, load_emumovies_credentials, obs_recording_status, save_emumovies_credentials
from parity_media import active_video, cleanup_duplicates, find_duplicate_media, load_media_queue
from parity_premium import apply_media_pack, download_gog_media, download_steam_trailer, list_media_packs, platform_categories, strings_for
from parity_saves import scan_all_saves
from webapp_state import DATA, JOB_MANAGER, MEDIA_JOB, MEDIA_TYPES_ALL, METADATA_DATABASE, PROCESS_LOCK, bump_media_epoch, download_image, game_from_payload, game_from_query, load_state_view, public_settings, transact_state


class MediaHandlers:
    def _api_get_api_media_audit(self, parsed):
        query = parse_qs(parsed.query)
        platform = query.get("platform", ["all"])[0]
        games = [
            game for game in load_state_view()["games"]
            if platform == "all" or game.get("platform") == platform
        ]
        self.send_json(200, {
            "games":len(games),
            "matched":sum(bool(game.get("launchbox_db_id")) for game in games),
            "missing_cover":sum(not Path(str(game.get("cover") or "")).is_file() for game in games),
            "missing_background":sum(not Path(str(game.get("background") or "")).is_file() for game in games),
            "missing_screenshots":sum(not any(Path(str(path)).is_file() for path in game.get("screenshots", []) if path) for game in games),
            "missing_box_back":sum(not Path(str(game.get("box_back") or "")).is_file() for game in games),
            "missing_box_spine":sum(not Path(str(game.get("box_spine") or "")).is_file() for game in games),
            "missing_box_3d":sum(not Path(str(game.get("box_3d") or "")).is_file() for game in games),
            "missing_clear_logo":sum(not Path(str(game.get("clear_logo") or "")).is_file() for game in games),
            "missing_fanart":sum(not Path(str(game.get("fanart") or "")).is_file() for game in games),
            "missing_banner":sum(not Path(str(game.get("banner") or "")).is_file() for game in games),
            "missing_icon":sum(not Path(str(game.get("icon") or "")).is_file() for game in games),
            "missing_title_screen":sum(not Path(str(game.get("title_screen") or "")).is_file() for game in games),
            "missing_cart_front":sum(not Path(str(game.get("cart_front") or "")).is_file() for game in games),
            "missing_cart_back":sum(not Path(str(game.get("cart_back") or "")).is_file() for game in games),
            "missing_disc":sum(not Path(str(game.get("disc") or "")).is_file() for game in games),
            "missing_advertisement":sum(not Path(str(game.get("advertisement") or "")).is_file() for game in games),
            "missing_manual":sum(not Path(str(game.get("manual") or "")).is_file() for game in games),
        })
        return

    def _api_get_api_media_bulk_status(self, parsed):
        with PROCESS_LOCK:
            job = dict(MEDIA_JOB)
        self.send_json(200, {"job":job})
        return

    def _api_get_api_ra_badge(self, parsed):
        query = parse_qs(parsed.query)
        name = re.sub(r"[^A-Za-z0-9_-]", "", query.get("name", [""])[0])
        locked = query.get("locked", ["0"])[0] == "1"
        if not name:
            raise BadgeNotFound("Badge not found")
            return
        badge = DATA.parent / "media/retroachievements/badges" / f"{name}{'_lock' if locked else ''}.png"
        try:
            if not badge.is_file():
                download_image(f"https://media.retroachievements.org/Badge/{badge.name}", badge)
            self.send_file(200, badge, "image/png")
        except (OSError, ValueError):
            raise BadgeNotFound("Badge not found") from None
        return

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
            if not media.is_file():
                raise FileNotFoundError
            self.send_file(200, media)
        except (KeyError, IndexError, ValueError, FileNotFoundError):
            raise MediaNotFound("Media not found") from None
        return

    def _api_get_api_media_duplicates(self, parsed):
        self.send_json(200, {"groups": find_duplicate_media(load_state_view()["games"])})
        return

    def _api_get_api_media_queue(self, parsed):
        self.send_json(200, {"queue": load_media_queue(DATA.parent / "media-queue.json")})
        return

    def _api_get_api_obs_status(self, parsed):
        self.send_json(200, obs_recording_status())
        return

    def _api_get_api_premium_strings(self, parsed):
        locale = parse_qs(parsed.query).get("locale", ["en"])[0]
        self.send_json(200, {"locale": locale, "strings": strings_for(locale)})
        return

    def _api_get_api_premium_media_packs(self, parsed):
        self.send_json(200, {"packs": list_media_packs(load_state_view().get("settings", {}))})
        return

    def _api_get_api_premium_platform_categories(self, parsed):
        self.send_json(200, {"categories": platform_categories(load_state_view().get("settings", {}))})
        return

    def _api_post_api_premium_media_packs_apply(self, payload):
        self.apply_media_pack_route(payload)

    def _api_post_api_metadata_trailer(self, payload):
        self.download_trailer(payload)

    def _api_post_api_metadata_gog(self, payload):
        self.download_gog_route(payload)

    def _api_post_api_media_bulk(self, payload):
        self.bulk_media(payload)

    def _api_post_api_bezels_download(self, payload):
        self.download_bezels(payload)

    def _api_post_api_emumovies_settings(self, payload):
        self.save_emumovies(payload)

    def _api_post_api_emumovies_download(self, payload):
        self.emumovies_download(payload)

    def _api_post_api_media_cleanup(self, payload):
        self.cleanup_media(payload)

    def _api_post_api_screenshot(self, payload):
        self.take_screenshot(payload)

    def _api_post_api_obs_attach(self, payload):
        self.obs_attach(payload)

    def _api_post_api_saves_scan_apply(self, payload):
        self.apply_save_scan(payload)

    def bulk_media(self, payload):
        media_types = payload.get("media", [])
        if not isinstance(media_types, list) or not media_types or not set(media_types) <= MEDIA_TYPES_ALL:
            raise ValueError("Select at least one valid media type.")
        if not METADATA_DATABASE.is_file():
            raise ValueError("Download the metadata database first.")
        platform = str(payload.get("platform", "all"))
        overwrite = bool(payload.get("overwrite"))
        with PROCESS_LOCK:
            if MEDIA_JOB.get("state") == "running":
                self.send_json(200, MEDIA_JOB)
                return
            MEDIA_JOB.clear()
            MEDIA_JOB.update({"state":"running", "current":0, "total":0, "updated":0, "errors":[]})

        def worker():
            state = load_state()
            targets = [
                (str(game.get("game_id")), str(game.get("launchbox_db_id")))
                for game in state["games"]
                if game.get("launchbox_db_id") and (platform == "all" or game.get("platform") == platform)
            ]
            with PROCESS_LOCK:
                MEDIA_JOB["total"] = len(targets)
            updated_count, errors = 0, []
            manual_missing = 0
            for current, (stable_id, database_id) in enumerate(targets, 1):
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
                    changes = {key:value for key,value in updated.items() if original.get(key) != value}
                    if changes:
                        def mutate(state, stable_id=stable_id, changes=changes):
                            game_from_payload(state, {"game_id": stable_id}).update(changes)
                        transact_state(mutate)
                        updated_count += 1
                except (OSError, ValueError, sqlite3.Error) as error:
                    errors.append(f"{original.get('name', stable_id)}: {error}")
                with PROCESS_LOCK:
                    MEDIA_JOB.update({"current":current, "updated":updated_count, "errors":errors[-20:], "manual_missing":manual_missing})
            bump_media_epoch()
            with PROCESS_LOCK:
                MEDIA_JOB["state"] = "done"

        JOB_MANAGER.submit("media-bulk", worker)
        self.send_json(202, {"state":"running"})

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
        groups = find_duplicate_media(load_state()["games"], allowed_roots=[DATA.parent])
        apply = bool(payload.get("apply"))
        deleted = cleanup_duplicates(groups, dry_run=not apply, allowed_roots=[DATA.parent])
        if apply and deleted:
            bump_media_epoch()
        self.send_json(200, {"groups": len(groups), "paths": deleted, "applied": apply})

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

    def apply_save_scan(self, payload):
        state = load_state()
        found = scan_all_saves(state["games"])
        found_by_id = {
            str(state["games"][index].get("game_id")): paths
            for index, paths in found.items()
            if 0 <= index < len(state["games"])
        }
        def mutate(state):
            updated = 0
            for stable_id, paths in found_by_id.items():
                try:
                    game = game_from_payload(state, {"game_id": stable_id})
                except IndexError:
                    continue
                save_paths = game.setdefault("save_paths", [])
                for path in paths:
                    if path not in save_paths:
                        save_paths.append(path)
                        updated += 1
            return updated
        _, updated = transact_state(mutate)
        self.send_json(200, {"updated": updated, "games": len(found)})


