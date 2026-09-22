"""LibraryHandlers capability handlers. Library, game CRUD, favorites, tags, queue, notifications, and health."""

import copy
from datetime import datetime
import os
import re
from pathlib import Path
import secrets
from urllib.parse import parse_qs

from api_errors import BadRequest, GameNotFound
from catalog import PROGRESS, bulk_update, canon_progress, clean_user_rating, game_media_paths, normalize_manual_sessions, normalize_notes, related_game_ids, tag_counts
from handlers._shared import clean_extras as _clean_extras_shared
from notifications import clear as clear_notifications, mark_read as mark_notifications_read, unread_count
from openbox import load_state, load_state_readonly, local_only_mutation
from routes.registry import route
from parity_deeplinks import launcher_menu_items
from parity_discovery import discovery_lists, related_with_reasons
from parity_filter_presets import bigbox_quick_presets, delete_preset, explorer_facets, list_presets, save_preset
from parity_media import normalize_video_fields
from parity_premium import bulk_wizard_changes, custom_field_defs, normalize_custom_fields
from pkg.parity.parity_duplicates import apply_merge, find_duplicates, merge_plan
from pkg.parity.parity_repair import apply_repair, plan_repair, scan_candidates as scan_repair_candidates, scan_missing_paths
from pkg.parity.parity_repair import resolve_folder as resolve_repair_folder
from play_queue import advance as advance_queue, enqueue as enqueue_queue, remove as remove_queue, reorder as reorder_queue, resolve_queue
from state_store import _stable_game_id, prune_trash
from webapp_state import FIELDS, MEDIA_PATH_FIELDS, _public_state_cached, approved_media_path, bump_media_epoch, clear_file_probe_cache, consolidate_existing_games, game_from_payload, game_from_query, load_state_view, public_state, public_state_bytes, public_state_etag, public_settings, transact_state


def _dna_note_upserted(games):
    """Incremental DNA index update after a library mutation (Flagship 9).

    Best-effort by design: the DNA index is derived data, so a failure
    here must never break the mutation that triggered it. Heavier drift
    (e.g. metadata refreshes, which are not hooked) is corrected by the
    reconciliation pass in handlers/discovery.py.
    """
    try:
        from pkg.parity import parity_dna

        index = parity_dna.load_index()
        if index is None:
            return
        for game in games:
            if isinstance(game, dict):
                parity_dna.note_game_upserted(index, game)
        parity_dna.save_index_atomic(index)
    except Exception:
        pass


def _dna_note_removed(game_ids):
    """Incremental DNA index removal after a library deletion (Flagship 9)."""
    try:
        from pkg.parity import parity_dna

        index = parity_dna.load_index()
        if index is None:
            return
        for game_id in game_ids:
            parity_dna.note_game_removed(index, str(game_id))
        parity_dna.save_index_atomic(index)
    except Exception:
        pass


def _dna_refresh_ids(game_ids):
    """Refresh DNA vectors for games identified by payload ids.

    Used after bulk mutations where only ids (not game dicts) are handy;
    resolves against a fresh readonly state.
    """
    try:
        wanted = {str(raw) for raw in game_ids or []}
        if not wanted:
            return
        state = load_state_readonly()
        matched = [
            game for game in state.get("games", [])
            if isinstance(game, dict)
            and (str(game.get("game_id")) in wanted or str(game.get("id")) in wanted)
        ]
        _dna_note_upserted(matched)
    except Exception:
        pass


def _clean_game_fields(source):
    game = {key: str(source[key]).strip() for key in FIELDS if key in source}
    game["extract_archive"] = bool(source.get("extract_archive"))
    game["hidden"] = bool(source.get("hidden"))
    for field in ("broken", "portable", "launch_confirm"):
        game[field] = bool(source.get(field))
    if "disc_count" in source:
        try:
            game["disc_count"] = max(0, int(source.get("disc_count") or 0))
        except (TypeError, ValueError) as error:
            raise ValueError("Disc count must be a number.") from error
    progress = canon_progress(game.get("progress", ""))
    if progress not in PROGRESS:
        raise ValueError("Unknown progress value.")
    # "Unplayed" is the UI label for unset; it is always stored as "".
    game["progress"] = progress
    try:
        game["rating"] = float(game.get("rating") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("Rating must be a number from 0 to 5.") from error
    if not 0 <= game["rating"] <= 5:
        raise ValueError("Rating must be between 0 and 5.")
    # F4b: personal star rating, distinct from the metadata ``rating`` float.
    if "user_rating" in source:
        game["user_rating"] = clean_user_rating(source.get("user_rating"))
    return game


def _apply_game_extras(game, applications, versions, documents):
    game["applications"] = applications
    game["versions"] = versions
    game["documents"] = documents
    for field in MEDIA_PATH_FIELDS:
        if game.get(field):
            game[field] = str(approved_media_path(game[field], must_exist=False))
    for document in game["documents"]:
        document["path"] = str(approved_media_path(document["path"], must_exist=False))
    return game


def _clean_game_lists(game, source):
    save_paths = source.get("save_paths", [])
    if not isinstance(save_paths, list):
        raise ValueError("Save paths must be a list.")
    game["save_paths"] = [str(path).strip() for path in save_paths if str(path).strip()][:50]
    screenshots = source.get("screenshots", [])
    if not isinstance(screenshots, list):
        raise ValueError("Screenshots must be a list.")
    game["screenshots"] = [
        str(approved_media_path(str(path).strip(), must_exist=False))
        for path in screenshots[:100] if str(path).strip()
    ]
    if "alternate_names" in source:
        names = source.get("alternate_names", [])
        if isinstance(names, str):
            game["alternate_names"] = [name.strip() for name in names.split(";") if name.strip()]
        elif isinstance(names, list):
            game["alternate_names"] = [str(name).strip() for name in names if str(name).strip()][:20]
    return game


def _clean_launch_env(value):
    """Validate per-game environment overrides into a bounded {KEY: value} dict.

    Accepts a mapping or newline-separated ``KEY=value`` text (the Edit game
    form serializes the textarea as text). Keys must be valid environment
    names; values are plain strings.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        pairs = {}
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if not sep:
                raise ValueError(f"Environment override must be KEY=value: {line!r}")
            pairs[key.strip()] = val.strip()
        value = pairs
    if not isinstance(value, dict):
        raise ValueError("Environment overrides must be an object or KEY=value lines.")
    if len(value) > 50:
        raise ValueError("At most 50 environment overrides per game.")
    clean = {}
    for key, val in value.items():
        key = str(key).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
        clean[key] = str(val)[:500]
    return clean


def _apply_game_misc(game, source):
    normalize_video_fields(game)
    if "launch_env" in source:
        game["launch_env"] = _clean_launch_env(source.get("launch_env"))
    game["hide_in_bigbox"] = bool(source.get("hide_in_bigbox"))
    esrb = str(source.get("esrb", game.get("esrb", ""))).strip()
    if esrb:
        game["esrb"] = esrb
    defs = custom_field_defs(load_state().get("settings", {}))
    if "custom_fields" in source and isinstance(source.get("custom_fields"), dict):
        game["custom_fields"] = {
            str(key).strip(): str(value).strip()
            for key, value in source["custom_fields"].items()
            if str(key).strip()
        }
        normalize_custom_fields(game, defs)
    return game


def _save_game_mutate(state, payload, game):
    if payload.get("id") is None and not payload.get("game_id"):
        game["added_at"] = datetime.now().isoformat(timespec="seconds")
        state["games"].append(game)
    else:
        existing = game_from_payload(state, payload)
        game["game_id"] = existing.get("game_id", game.get("game_id", ""))
        existing.update(game)


# ── Trash bin (S3) ───────────────────────────────────────────────────────────
# v2 delete is a soft delete: the full record plus its manual-playlist
# memberships move into the bounded state["trash"] list so the toast's Undo and
# the trash view's Restore can put it back. v1 /api/game/delete stays a hard
# delete (frozen contract). Bounds (TRASH_CAP, TRASH_MAX_AGE_DAYS) are enforced
# on every write via state_store.prune_trash and again at load time.
def _trash_entry_for(state, game):
    """Build the trash entry preserving the full record and memberships."""
    game_id = str(game.get("game_id") or "")
    member_keys = {game_id, str(game.get("id") or "")} - {""}
    playlists = [
        str(playlist.get("name") or "")
        for playlist in state.get("playlists", [])
        if playlist.get("type") == "manual"
        and isinstance(playlist.get("members"), list)
        and any(str(member) in member_keys for member in playlist["members"])
    ]
    # Identity scan: equal-content games must not alias the original position.
    index = next((i for i, item in enumerate(state["games"]) if item is game), len(state["games"]))
    return {
        "trash_id": f"trash-{secrets.token_hex(6)}",
        "trashed_at": datetime.now().isoformat(timespec="seconds"),
        "index": index,
        "game_id": game_id,
        "name": str(game.get("name") or ""),
        "platform": str(game.get("platform") or ""),
        "game": copy.deepcopy(game),
        "playlists": playlists,
    }


def _trash_entry_public(entry):
    """Project a trash entry for the trash view payload."""
    game = entry.get("game") if isinstance(entry.get("game"), dict) else {}
    return {
        "trash_id": str(entry.get("trash_id") or ""),
        "game_id": str(entry.get("game_id") or game.get("game_id") or ""),
        "name": str(entry.get("name") or game.get("name") or ""),
        "platform": str(entry.get("platform") or game.get("platform") or ""),
        "trashed_at": str(entry.get("trashed_at") or ""),
        "playlists": [str(name) for name in entry.get("playlists") or []],
    }


def _free_game_id(used, base):
    """Return base, or the first free ``base-N``, for a restored id collision."""
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _remove_media_files(media_paths, referenced_media):
    """Delete media files no remaining game references; mirrors v1 delete semantics."""
    deleted_media = []
    shared_media = []
    for path in media_paths:
        try:
            target = approved_media_path(path, must_exist=True)
            canon = os.path.realpath(str(target))
            if canon in referenced_media:
                if str(target) not in shared_media:
                    shared_media.append(str(target))
                continue
            if target.is_file():
                target.unlink()
                if str(target) not in deleted_media:
                    deleted_media.append(str(target))
        except (OSError, ValueError):
            pass
    return deleted_media, shared_media


class LibraryHandlers:
    @route("GET", "/api/library")
    def _api_get_api_library(self, parsed):
        # Check for pagination parameters before applying the full-library ETag.
        # A page is a different representation even when the underlying state
        # has not changed.
        query_params = parse_qs(parsed.query)
        offset_str = query_params.get("offset", [None])[0]
        limit_str = query_params.get("limit", [None])[0]
        etag = public_state_etag()
        if (
            offset_str is None
            and limit_str is None
            and etag
            and self.headers.get("If-None-Match", "").strip() == etag
        ):
            self.send_response(304)
            self.headers_common("application/json; charset=utf-8", "private, no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            return
        
        if offset_str is not None or limit_str is not None:
            # Paginated response
            try:
                offset = int(offset_str) if offset_str else 0
                limit = int(limit_str) if limit_str else 500
                if offset < 0 or limit < 0 or limit > 5000:
                    raise ValueError("Invalid pagination parameters")
            except (ValueError, TypeError):
                raise BadRequest("Invalid pagination parameters") from None
            payload = public_state()
            total_count = len(payload["games"])
            paginated_games = payload["games"][offset:offset + limit]
            
            response_payload = {
                "games": paginated_games,
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
                "playlists": payload.get("playlists", []),
                "filter_presets": payload.get("filter_presets", []),
                "settings": payload.get("settings", {}),
                "media_epoch": payload.get("media_epoch", 0),
                "ra_configured": payload.get("ra_configured", False),
            }
            self.send_json(200, response_payload)
            return
        
        # Non-paginated response (backward compatible)
        data = public_state_bytes()
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            compressed = _public_state_cached()["raw_gzip"]
            if compressed is not None and len(compressed) < len(data):
                self.send_bytes(
                    200, compressed, "application/json; charset=utf-8",
                    cache_control="private, no-cache", etag=etag,
                    extra_headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                )
                return
        self.send_bytes(
            200, data, "application/json; charset=utf-8",
            cache_control="private, no-cache", etag=etag,
        )
        return

    @route("GET", "/api/library/delta")
    def _api_get_api_library_delta(self, parsed):
        """Return only the specified games by ID for incremental updates in O(K) time."""
        query_params = parse_qs(parsed.query)
        ids_str = query_params.get("ids", [None])[0]
        
        if not ids_str:
            raise BadRequest("Missing ids parameter")
        
        try:
            ids = [id_str.strip() for id_str in ids_str.split(",") if id_str.strip()]
            if not ids:
                raise BadRequest("Missing ids parameter")
            if len(ids) > 1000:
                raise BadRequest("Too many IDs (max 1000)")
        except (AttributeError, TypeError):
            raise BadRequest("Invalid IDs format") from None
        
        cached_info = _public_state_cached()
        games_by_id = cached_info.get("games_by_id")
        if games_by_id is None:
            payload = cached_info["payload"]
            games_by_id = {}
            for game in payload["games"]:
                gid = str(game.get("game_id") or "")
                if gid:
                    games_by_id[gid] = game
                games_by_id[str(game.get("id"))] = game
        
        seen = set()
        delta_games = []
        for id_val in ids:
            game = games_by_id.get(id_val)
            if game is not None and id(game) not in seen:
                seen.add(id(game))
                delta_games.append(game)
        
        response_payload = {
            "games": delta_games,
            "media_epoch": cached_info["payload"].get("media_epoch", 0),
        }
        self.send_json(200, response_payload)
        return

    @route("GET", "/api/related")
    def _api_get_api_related(self, parsed):
        try:
            query = parse_qs(parsed.query)
            state = load_state_view()
            index = state["games"].index(game_from_query(state, query))
            related = related_game_ids(state["games"], index)
            self.send_json(200, {"ids": related})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/discovery")
    def _api_get_api_discovery(self, parsed):
        self.send_json(200, discovery_lists(load_state_view()["games"]))
        return

    @route("GET", "/api/related/rich")
    def _api_get_api_related_rich(self, parsed):
        state = load_state_view()
        query = parse_qs(parsed.query)
        try:
            game = game_from_query(state, query)
            index = state["games"].index(game)
            self.send_json(200, {"items": related_with_reasons(state["games"], index)})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    @route("GET", "/api/filter-presets")
    def _api_get_api_filter_presets(self, parsed):
        state = load_state_view()
        self.send_json(200, {
            "presets": list_presets(state),
            "bigbox_quick": bigbox_quick_presets(state),
        })
        return

    @route("GET", "/api/explorer/facets")
    def _api_get_api_explorer_facets(self, parsed):
        field = parse_qs(parsed.query).get("field", ["genre"])[0]
        state = load_state_view()
        # Keep the established explorer contract (hidden filtering, split
        # genres, blank-value labels, and tie ordering) independent of the
        # optional SQLite acceleration path.
        self.send_json(200, {"field": field, "facets": explorer_facets(state["games"], field)})
        return

    @route("GET", "/api/launcher/menu")
    def _api_get_api_launcher_menu(self, parsed):
        payload = public_state()
        self.send_json(200, {"items": launcher_menu_items(payload["games"])})
        return

    @route("GET", "/api/queue")
    def _api_get_api_queue(self, parsed):
        self.send_json(200, {"queue": resolve_queue(load_state_view())})
        return

    @route("GET", "/api/notifications")
    def _api_get_api_notifications(self, parsed):
        state = load_state_view()
        self.send_json(200, {"notifications": state.get("notifications", []), "unread": unread_count(state)})
        return

    @route("GET", "/api/tags")
    def _api_get_api_tags(self, parsed):
        self.send_json(200, {"tags": tag_counts(load_state_view()["games"])})
        return

    @route("POST", "/api/favorite")
    def _api_post_api_favorite(self, payload):
        self.favorite(payload)

    @route("POST", "/api/game")
    def _api_post_api_game(self, payload):
        self.save_game(payload)

    @route("POST", "/api/game/delete")
    def _api_post_api_game_delete(self, payload):
        self.delete_game(payload)

    @route("POST", "/api/games/delete-steam")
    def _api_post_api_games_delete_steam(self, payload):
        self.delete_steam_games(payload)

    @route("POST", "/api/games/bulk")
    def _api_post_api_games_bulk(self, payload):
        self.bulk_edit(payload)

    @route("POST", "/api/games/bulk-wizard")
    def _api_post_api_games_bulk_wizard(self, payload):
        self.bulk_wizard(payload)

    @route("POST", "/api/queue")
    def _api_post_api_queue(self, payload):
        self.queue(payload)

    @route("POST", "/api/notifications")
    def _api_post_api_notifications(self, payload):
        self.notifications(payload)

    @route("POST", "/api/tags")
    def _api_post_api_tags(self, payload):
        self.tags(payload)

    @route("POST", "/api/image-group")
    def _api_post_api_image_group(self, payload):
        self.save_image_group(payload)

    @route("POST", "/api/filter-presets")
    def _api_post_api_filter_presets(self, payload):
        self.save_filter_preset(payload)

    @route("POST", "/api/filter-presets/delete")
    def _api_post_api_filter_presets_delete(self, payload):
        self.delete_filter_preset(payload)

    @route("POST", "/api/health")
    def _api_post_api_health(self, payload):
        self.health()

    @route("POST", "/api/health/dedupe")
    def _api_post_api_health_dedupe(self, payload):
        self.dedupe()

    def favorite(self, payload):
        def mutate(state):
            game = game_from_payload(state, payload)
            game["favorite"] = not game.get("favorite", False)
            return game["favorite"]
        _, favorite = transact_state(local_only_mutation(mutate))
        self.send_json(200, {"favorite": favorite})

    def queue(self, payload):
        action = str(payload.get("action") or "list")
        def mutate(state):
            if action == "enqueue":
                enqueue_queue(state, payload.get("game_ids", []), payload.get("position"), payload.get("note", ""))
            elif action == "remove":
                remove_queue(state, payload.get("game_ids", []))
            elif action == "reorder":
                reorder_queue(state, payload.get("ordered_game_ids", []))
            elif action == "advance":
                return advance_queue(state, payload.get("current_game_id"))
            elif action not in {"list", "resolve"}:
                raise ValueError("Unknown queue action.")
            return None
        _, result = transact_state(mutate)
        self.send_json(200, {"queue": resolve_queue(load_state()), "next": result if action == "advance" else None})

    def notifications(self, payload):
        action = str(payload.get("action") or "list")
        def mutate(state):
            if action == "read":
                mark_notifications_read(state, payload.get("ids"))
            elif action == "clear":
                clear_notifications(state, payload.get("ids"))
            elif action != "list":
                raise ValueError("Unknown notification action.")
            return unread_count(state)
        committed, unread = transact_state(mutate)
        self.send_json(200, {"notifications": committed.get("notifications", []), "unread": unread})

    def tags(self, payload):
        ids = payload.get("ids")
        changes = {key: payload[key] for key in ("tags", "tags_add", "tags_remove") if key in payload}
        if not changes:
            raise ValueError("No tag changes were supplied.")
        def mutate(state):
            return bulk_update(state["games"], ids, changes)
        _, updated = transact_state(mutate)
        _dna_refresh_ids(payload.get("ids"))
        self.send_json(200, {"updated": updated, "tags": tag_counts(load_state()["games"])})

    def save_game(self, payload):
        source = payload.get("game", {})
        game = _clean_game_fields(source)
        _apply_game_extras(
            game,
            self.clean_extras(source.get("applications", []), command=True),
            self.clean_extras(source.get("versions", []), command=True),
            self.clean_extras(source.get("documents", []), command=False),
        )
        _clean_game_lists(game, source)
        _apply_game_misc(game, source)
        if not game.get("name"):
            raise ValueError("Name is required.")
        game_path = str(game.get("path", "")).strip()
        if not game_path:
            raise ValueError("Path is required.")
        candidate = Path(game_path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Game path must be an absolute path.")
        # Reject symlinked components to avoid TOCTOU and path confusion;
        # resolve without strict to check the would-be target.
        cursor = candidate
        while True:
            try:
                if cursor.is_symlink():
                    raise ValueError("Game path may not contain symlinks.")
            except OSError as error:
                raise ValueError("Could not inspect game path.") from error
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        if not candidate.exists():
            raise ValueError("Path must point to an existing local file.")
        if not candidate.is_file():
            raise ValueError("Game path must be a regular file.")
        # Store the expanded absolute form
        game["path"] = str(candidate)
        def mutate(state):
            _save_game_mutate(state, payload, game)
        transact_state(mutate)
        clear_file_probe_cache()
        _dna_note_upserted([game])
        self.send_json(200, {"ok": True})

    def bulk_edit(self, payload):
        def mutate(state):
            return bulk_update(state["games"], payload.get("ids"), payload.get("changes"))
        _, changed = transact_state(mutate)
        _dna_refresh_ids(payload.get("ids"))
        self.send_json(200, {"updated": changed})

    def delete_game(self, payload):
        delete_media = bool(payload.get("delete_media"))
        media_paths = []
        referenced_media = set()
        
        def mutate(state):
            game = game_from_payload(state, payload)
            if delete_media:
                media_paths.extend(game_media_paths(game))
            state["games"].remove(game)
            if delete_media:
                for other_game in state["games"]:
                    for path in game_media_paths(other_game):
                        try:
                            referenced_media.add(os.path.realpath(str(path)))
                        except Exception:
                            pass
            return game.get("name", ""), str(game.get("game_id", ""))
            
        _, (removed, removed_id) = transact_state(mutate)

        deleted_media = []
        shared_media = []

        if delete_media:
            deleted_media, shared_media = _remove_media_files(media_paths, referenced_media)
            bump_media_epoch()

        clear_file_probe_cache()
        if removed_id:
            _dna_note_removed([removed_id])
        self.send_json(200, {
            "removed": removed,
            "deleted_media": deleted_media,
            "shared_media": shared_media
        })

    def delete_steam_games(self, payload):
        def mutate(state):
            games = state["games"]
            removed_ids = [str(game.get("game_id", "")) for game in games
                           if str(game.get("source", "")).casefold() == "steam"]
            state["games"] = [game for game in games if str(game.get("source", "")).casefold() != "steam"]
            return len(games) - len(state["games"]), removed_ids
        _, (removed, removed_ids) = transact_state(mutate)
        _dna_note_removed([game_id for game_id in removed_ids if game_id])
        self.send_json(200, {"removed": removed})

    @staticmethod
    def clean_extras(items, command):
        return _clean_extras_shared(items, command)

    def save_image_group(self, payload):
        group = str(payload.get("group", ""))
        scope = str(payload.get("scope", "global"))
        name = str(payload.get("name", "")).strip()
        if group not in {"default", "cover", "background", "screenshot", "clear_logo", "fanart", "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back", "disc", "advertisement", "manual"} or scope not in {"global", "platform", "playlist"}:
            raise ValueError("Unknown image group.")
        if scope != "global" and (not name or len(name) > 200):
            raise ValueError("A platform or playlist is required.")
        def mutate(state):
            settings = state.setdefault("settings", {})
            if scope == "global":
                settings["image_group"] = "cover" if group == "default" else group
            else:
                mappings = settings.setdefault(f"image_group_by_{scope}", {})
                if group == "default":
                    mappings.pop(name, None)
                else:
                    mappings[name] = group
        state = transact_state(mutate)[0]
        self.send_json(200, public_settings(state))

    def bulk_wizard(self, payload):
        changes = bulk_wizard_changes(payload.get("changes", {}))
        def mutate(state):
            return bulk_update(state["games"], payload.get("ids"), changes)
        _, changed = transact_state(mutate)
        _dna_refresh_ids(payload.get("ids"))
        self.send_json(200, {"updated": changed, "fields": list(changes.keys())})

    def save_filter_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        rules = payload.get("rules", {})
        bigbox_quick = bool(payload.get("bigbox_quick", False))
        def mutate(state):
            save_preset(state, name, rules, bigbox_quick=bigbox_quick)
        transact_state(mutate)
        self.send_json(200, {"saved": name})

    def delete_filter_preset(self, payload):
        name = str(payload.get("name", "")).strip()
        def mutate(state):
            if not delete_preset(state, name):
                raise ValueError("Preset not found.")
        transact_state(mutate)
        self.send_json(200, {"deleted": name})

    def health(self):
        # Detection lives in pkg.parity.parity_library_health so the v1 audit
        # and the health score can never disagree. The response shape below is
        # pinned by scripts/check_v1_contract.py: do not change it.
        from pkg.parity.parity_library_health import detect_issues
        state = load_state()
        detected = detect_issues(state["games"], state)
        legacy = [issue for issue in detected if issue.get("legacy")]
        self.send_json(200, {
            "games": len(state["games"]),
            "missing": sum(issue["legacy"]["type"] == "Missing game" for issue in legacy),
            "duplicates": sum(issue["legacy"]["type"] == "Duplicate" for issue in legacy),
            "unconfigured": sum(not game.get("path") and not game.get("manual_entry") for game in state["games"]),
            "missing_media": sum(issue["legacy"]["type"] == "Missing box front" for issue in legacy),
            "issues": [
                {
                    "id": issue["index"],
                    "game": issue["name"],
                    "type": issue["legacy"]["type"],
                    "detail": issue["legacy"]["detail"],
                }
                for issue in legacy
            ],
        })

    def dedupe(self):
        def mutate(state):
            return consolidate_existing_games(state["games"])
        _, removed = transact_state(mutate)
        self.send_json(200, {"removed": removed})

    # ── Missing-file repair wizard ───────────────────────────────────────────
    @route("GET", "/api/v2/library/repair")
    def _api_get_api_v2_library_repair(self, parsed):
        """List missing game/media paths without mutating anything."""
        qs = parse_qs(parsed.query or "")
        include_media = (qs.get("media", ["1"])[0] or "1").strip().lower() not in {"0", "false", "no"}
        self.send_json(200, scan_missing_paths(load_state_view(), include_media=include_media))

    @route("POST", "/api/v2/library/repair/preview")
    def _api_post_api_v2_library_repair_preview(self, payload):
        """Dry-run: match missing basenames against a user-picked folder."""
        include_media, fields, folder = self._repair_request(payload)
        candidates = scan_repair_candidates(folder)
        scan = scan_missing_paths(load_state_view(), include_media=include_media)
        plan = plan_repair(scan["items"], candidates, fields=fields)
        plan["folder"] = str(folder)
        plan["scanned"] = scan["count"]
        plan["candidates"] = len(candidates)
        self.send_json(200, plan)

    @route("POST", "/api/v2/library/repair/apply")
    def _api_post_api_v2_library_repair_apply(self, payload):
        """Re-plan inside the transaction and relink only still-missing rows."""
        include_media, fields, folder = self._repair_request(payload)
        selection = payload.get("selection")
        if selection is not None and not isinstance(selection, list):
            raise BadRequest("selection must be a list of [id, field] pairs or ids.")
        candidates = scan_repair_candidates(folder)

        def mutate(state):
            scan = scan_missing_paths(state, include_media=include_media)
            plan = plan_repair(scan["items"], candidates, fields=fields)
            return apply_repair(state, plan["matches"], selection=selection)

        _, result = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, result)

    def _repair_request(self, payload):
        payload = payload or {}
        try:
            folder = resolve_repair_folder(payload.get("folder"))
        except ValueError as error:
            raise BadRequest(str(error)) from error
        fields = payload.get("fields")
        if fields is not None and not isinstance(fields, list):
            raise BadRequest("fields must be a list of field names.")
        return bool(payload.get("include_media", True)), fields, folder

    # ── Duplicate detection & merge ───────────────────────────────────────
    @route("GET", "/api/v2/library/duplicates")
    def _api_get_api_v2_library_duplicates(self, parsed):
        qs = parse_qs(parsed.query or "")
        include_title = (qs.get("title", ["1"])[0] or "1").strip().lower() not in {"0", "false", "no"}
        self.send_json(200, find_duplicates(load_state_view(), include_title=include_title))

    @route("POST", "/api/v2/library/duplicates/preview")
    def _api_post_api_v2_library_duplicates_preview(self, payload):
        indexes = (payload or {}).get("ids")
        if not isinstance(indexes, list):
            raise BadRequest("ids must be a list of game indexes.")
        try:
            plan = merge_plan(load_state_view(), indexes)
        except ValueError as error:
            raise BadRequest(str(error)) from error
        self.send_json(200, plan)

    @route("POST", "/api/v2/library/duplicates/merge")
    def _api_post_api_v2_library_duplicates_merge(self, payload):
        indexes = (payload or {}).get("ids")
        if not isinstance(indexes, list) or len(indexes) < 2:
            raise BadRequest("A merge needs at least two game indexes.")

        def mutate(state):
            return apply_merge(state, indexes, trash_game=_trash_entry_for)

        _, result = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, result)

    @route("POST", "/api/v2/library/manual-entry")
    def _api_post_api_v2_library_manual_entry(self, payload):
        """Add a manual/shelf entry for a game without a local file path.

        Reuses the existing game field infrastructure. Only name is required;
        platform, genre, developer, etc. are optional. The entry is marked
        with ``manual_entry: true`` so it can be filtered or displayed
        differently in the UI.
        """
        source = payload.get("game", {})
        game = _clean_game_fields(source)
        if not game.get("name"):
            raise BadRequest("Name is required.")
        game["manual_entry"] = True
        game["path"] = ""  # manual entries have no executable path
        _clean_game_lists(game, source)
        _apply_game_misc(game, source)

        def mutate(state):
            if "games" not in state:
                state["games"] = []
            state["games"].append(game)

        transact_state(mutate)
        clear_file_probe_cache()
        _dna_note_upserted([game])
        self.send_json(200, {"ok": True, "name": game.get("name")})

    @route("POST", "/api/v2/library/manual-entry/convert")
    def _api_post_api_v2_library_manual_entry_convert(self, payload):
        """Attach a verified local file to a shelf entry without changing its identity."""
        path_value = str(payload.get("path") or "").strip()
        if not path_value:
            raise BadRequest("path is required to convert a shelf entry.")
        candidate = Path(path_value).expanduser()
        if not candidate.is_absolute() or not candidate.is_file():
            raise BadRequest("path must be an existing absolute file.")

        def mutate(state):
            game = game_from_payload(state, payload)
            if not game.get("manual_entry"):
                raise BadRequest("Game is not a shelf entry.")
            game["path"] = str(candidate)
            game["manual_entry"] = False
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"ok": True, "game_id": game_id, "playable": True})

    @route("POST", "/api/v2/library/manual-entry/update")
    def _api_post_api_v2_library_manual_entry_update(self, payload):
        """Update a shelf entry while retaining its stable identity and pathless type."""
        source = payload.get("game", {})
        game = _clean_game_fields(source)
        if not game.get("name"):
            raise BadRequest("Name is required.")
        _clean_game_lists(game, source)
        _apply_game_misc(game, source)

        def mutate(state):
            existing = game_from_payload(state, payload)
            if not existing.get("manual_entry"):
                raise BadRequest("Game is not a shelf entry.")
            game["game_id"] = existing.get("game_id", game.get("game_id", ""))
            game["manual_entry"] = True
            game["path"] = ""
            existing.update(game)
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        clear_file_probe_cache()
        _dna_refresh_ids([game_id])
        self.send_json(200, {"ok": True, "game_id": game_id, "manual_entry": True})

    # ── Backlog management (F4) v2 routes ──────────────────────────────────
    # Playtime logging (F4c), per-game notes (F4d), and single-field setters
    # for progress (F4a) / personal rating (F4b). The /manual-entry namespace
    # is reserved for shelf entries; manual playtime lives under /playtime.

    @staticmethod
    def _clean_playtime_entry(seconds, date=None, note=None):
        """Validate one manual playtime entry; returns a bounded dict."""
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            raise BadRequest("seconds must be an integer.") from None
        if seconds <= 0 or seconds > 24 * 3600:
            raise BadRequest("seconds must be between 1 and 86400.")
        date = str(date or "").strip()
        if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise BadRequest("date must be YYYY-MM-DD.")
        note = str(note or "").strip()
        if len(note) > 200:
            raise BadRequest("note is limited to 200 characters.")
        return {"date": date, "seconds": seconds, "note": note}

    @staticmethod
    def _resync_manual_playtime(game):
        """Recompute the additive total from the normalized session list."""
        sessions = normalize_manual_sessions(game.get("manual_sessions"))
        game["manual_sessions"] = sessions
        game["manual_playtime_seconds"] = sum(entry["seconds"] for entry in sessions)

    @route("POST", "/api/v2/library/playtime/log")
    def _api_post_api_v2_library_playtime_log(self, payload):
        """Append a manual playtime entry; totals include it (badged manual)."""
        entry = self._clean_playtime_entry(
            payload.get("seconds"), date=payload.get("date"), note=payload.get("note"))

        def mutate(state):
            game = game_from_payload(state, payload)
            sessions = normalize_manual_sessions(game.get("manual_sessions"))
            sessions.append(entry)
            game["manual_sessions"] = sessions
            self._resync_manual_playtime(game)
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id, "entry": entry})

    @route("POST", "/api/v2/library/playtime/update")
    def _api_post_api_v2_library_playtime_update(self, payload):
        """Replace one manual playtime entry by index."""
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            raise BadRequest("index must be an integer.") from None
        entry = self._clean_playtime_entry(
            payload.get("seconds"), date=payload.get("date"), note=payload.get("note"))

        def mutate(state):
            game = game_from_payload(state, payload)
            sessions = normalize_manual_sessions(game.get("manual_sessions"))
            if not 0 <= index < len(sessions):
                raise BadRequest("Unknown playtime entry.")
            sessions[index] = entry
            game["manual_sessions"] = sessions
            self._resync_manual_playtime(game)
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id, "entry": entry})

    @route("POST", "/api/v2/library/playtime/delete")
    def _api_post_api_v2_library_playtime_delete(self, payload):
        """Delete one manual playtime entry by index."""
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            raise BadRequest("index must be an integer.") from None

        def mutate(state):
            game = game_from_payload(state, payload)
            sessions = normalize_manual_sessions(game.get("manual_sessions"))
            if not 0 <= index < len(sessions):
                raise BadRequest("Unknown playtime entry.")
            sessions.pop(index)
            game["manual_sessions"] = sessions
            self._resync_manual_playtime(game)
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id})

    @route("POST", "/api/v2/library/notes/add")
    def _api_post_api_v2_library_notes_add(self, payload):
        """Append a dated note entry; legacy string notes migrate on read."""
        text = str(payload.get("text") or "").strip()
        if not text:
            raise BadRequest("Note text is required.")
        if len(text) > 2000:
            raise BadRequest("Note text is limited to 2000 characters.")
        stamp = datetime.now().isoformat(timespec="seconds")

        def mutate(state):
            game = game_from_payload(state, payload)
            entries = normalize_notes(game.get("notes"))
            entries.append({"ts": stamp, "text": text})
            game["notes"] = entries
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id})

    @route("POST", "/api/v2/library/notes/update")
    def _api_post_api_v2_library_notes_update(self, payload):
        """Replace one note entry's text by index (keeps its timestamp)."""
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            raise BadRequest("index must be an integer.") from None
        text = str(payload.get("text") or "").strip()
        if not text:
            raise BadRequest("Note text is required.")
        if len(text) > 2000:
            raise BadRequest("Note text is limited to 2000 characters.")

        def mutate(state):
            game = game_from_payload(state, payload)
            entries = normalize_notes(game.get("notes"))
            if not 0 <= index < len(entries):
                raise BadRequest("Unknown note entry.")
            entries[index] = {"ts": entries[index].get("ts", ""), "text": text}
            game["notes"] = entries
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id})

    @route("POST", "/api/v2/library/notes/delete")
    def _api_post_api_v2_library_notes_delete(self, payload):
        """Delete one note entry by index."""
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            raise BadRequest("index must be an integer.") from None

        def mutate(state):
            game = game_from_payload(state, payload)
            entries = normalize_notes(game.get("notes"))
            if not 0 <= index < len(entries):
                raise BadRequest("Unknown note entry.")
            entries.pop(index)
            game["notes"] = entries
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id})

    @route("POST", "/api/v2/library/progress/set")
    def _api_post_api_v2_library_progress_set(self, payload):
        """Set one game's progress (F4a). "Unplayed" stores as "" (unset)."""
        progress = canon_progress(payload.get("progress", ""))
        if progress not in PROGRESS:
            raise BadRequest("Unknown progress value.")

        def mutate(state):
            game = game_from_payload(state, payload)
            game["progress"] = progress
            game["progress_suggested"] = True  # a deliberate choice ends the auto-suggest
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id, "progress": progress})

    @route("POST", "/api/v2/library/rating/set")
    def _api_post_api_v2_library_rating_set(self, payload):
        """Set one game's personal star rating 0-5 (F4b); 0 clears it."""
        try:
            user_rating = clean_user_rating(payload.get("user_rating"))
        except ValueError as error:
            raise BadRequest(str(error)) from None

        def mutate(state):
            game = game_from_payload(state, payload)
            game["user_rating"] = user_rating
            return game.get("game_id", "")

        _, game_id = transact_state(mutate)
        self.send_json(200, {"ok": True, "game_id": game_id, "user_rating": user_rating})

    @route("GET", "/api/v2/library/search")
    def _api_get_api_v2_library_search(self, parsed):
        params = parse_qs(parsed.query, keep_blank_values=True)
        query = params.get("q", [""])[0]
        try:
            limit = int(params.get("limit", ["50"])[0])
        except (TypeError, ValueError):
            raise BadRequest("Invalid limit.") from None
        limit = max(1, min(limit, 200))
        if not query.strip():
            self.send_json(200, {"results": [], "source": "json", "count": 0})
            return
        from pkg.state.cache import SQLITE_READ_MODEL
        # Above SQLITE_AUTO_THRESHOLD games the read model self-enables
        # (1.12.0); the explicit env opt-out is still honored.
        readonly = load_state_readonly()
        if SQLITE_READ_MODEL.enabled or SQLITE_READ_MODEL.should_auto_enable(len(readonly.get("games", []) or [])):
            state = load_state_view()
            import openbox
            SQLITE_READ_MODEL.ensure_fresh(state, openbox.STATE_STORE.signature())
            results = SQLITE_READ_MODEL.search(query, limit=limit)
            self.send_json(200, {"results": results[:limit], "source": "sqlite", "count": len(results)})
            return
        # JSON fallback: canonical name substring match, preserving library
        # order and the existing policy of returning hidden games as well.
        state = readonly
        q_lower = query.casefold()
        results = []
        for game in state.get("games", []):
            if q_lower in str(game.get("name", "")).casefold():
                # The state store owns this cached object; detach only the
                # bounded response rows rather than deep-copying the whole
                # library on every search request.
                results.append(dict(game))
                if len(results) >= limit:
                    break
        self.send_json(200, {"results": results, "source": "json", "count": len(results)})
        return

    @route("POST", "/api/v2/library/query/parse")
    def _api_post_api_v2_library_query_parse(self, payload):
        """Parse a natural-language query without changing the library."""
        if not isinstance(payload, dict):
            raise BadRequest("Request body must be an object.")
        query = payload.get("query")
        if not isinstance(query, str):
            raise BadRequest("query must be a string.")
        if len(query) > 2000:
            raise BadRequest("query is too long.")

        from pkg.parity import parity_query

        parsed = parity_query.parse_query(query)
        state = load_state_view()
        matches = parity_query.filter_games_by_query(state.get("games", []), parsed)
        # Let the client apply the exact interpretation without shipping
        # duplicate game records. Keep the identifier payload bounded while
        # preserving the full count for honest result messaging.
        matched_game_ids = [
            str(game.get("game_id") or game.get("id") or "")
            for game in matches[:20000]
            if isinstance(game, dict) and (game.get("game_id") or game.get("id")) is not None
        ]
        self.send_json(200, {
            **parsed,
            "match_count": len(matches),
            "matched_game_ids": matched_game_ids,
        })

    # ── Trash bin routes (S3) — see module-level helpers above ─────────────
    @route("GET", "/api/v2/library/trash")
    def _api_get_api_v2_library_trash(self, parsed):
        """List trash entries, newest first."""
        state = load_state_view()
        items = [_trash_entry_public(entry) for entry in reversed(state.get("trash") or [])]
        self.send_json(200, {"items": items, "count": len(items)})

    @route("POST", "/api/v2/library/trash")
    def _api_post_api_v2_library_trash(self, payload):
        """Move a game into the trash bin (v2 soft delete; undoable)."""
        delete_media = bool(payload.get("delete_media"))
        media_paths = []
        referenced_media = set()

        def mutate(state):
            game = game_from_payload(state, payload)
            if delete_media:
                media_paths.extend(game_media_paths(game))
            entry = _trash_entry_for(state, game)
            state["games"].remove(game)
            member_keys = {entry["game_id"], str(game.get("id") or "")} - {""}
            for playlist in state.get("playlists", []):
                if playlist.get("type") != "manual" or not isinstance(playlist.get("members"), list):
                    continue
                playlist["members"] = [member for member in playlist["members"] if str(member) not in member_keys]
            if delete_media:
                for other_game in state["games"]:
                    for path in game_media_paths(other_game):
                        try:
                            referenced_media.add(os.path.realpath(str(path)))
                        except Exception:
                            pass
            trash = state.setdefault("trash", [])
            trash.append(entry)
            state["trash"] = prune_trash(trash)
            return entry

        _, entry = transact_state(mutate)

        deleted_media = []
        shared_media = []
        if delete_media:
            deleted_media, shared_media = _remove_media_files(media_paths, referenced_media)
            bump_media_epoch()

        clear_file_probe_cache()
        self.send_json(200, {
            "ok": True,
            "trash_id": entry["trash_id"],
            "game_id": entry["game_id"],
            "name": entry["name"],
            "deleted_media": deleted_media,
            "shared_media": shared_media,
        })

    @route("POST", "/api/v2/library/trash/restore")
    def _api_post_api_v2_library_trash_restore(self, payload):
        """Restore a trash entry into the library with identity and playlists."""
        trash_id = str(payload.get("trash_id") or "").strip()
        if not trash_id:
            raise BadRequest("trash_id is required.")

        def mutate(state):
            entry = next(
                (item for item in state.get("trash") or []
                 if isinstance(item, dict) and item.get("trash_id") == trash_id),
                None,
            )
            if entry is None:
                raise BadRequest("Trash entry not found.")
            game = entry.get("game") if isinstance(entry.get("game"), dict) else {}
            restored = copy.deepcopy(game)
            used = {str(item.get("game_id") or "") for item in state.get("games", [])}
            original = str(entry.get("game_id") or restored.get("game_id") or "")
            wanted = original or _stable_game_id(restored)
            restored["game_id"] = _free_game_id(used, wanted)
            renamed = restored["game_id"] != original
            index = entry.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index > len(state["games"]):
                index = len(state["games"])
            state["games"].insert(index, restored)
            for name in entry.get("playlists") or []:
                for playlist in state.get("playlists", []):
                    if playlist.get("type") != "manual" or playlist.get("name") != name:
                        continue
                    members = playlist.get("members")
                    if not isinstance(members, list):
                        members = []
                        playlist["members"] = members
                    if restored["game_id"] not in members:
                        members.append(restored["game_id"])
            state["trash"] = [item for item in state.get("trash") or [] if item is not entry]
            return {"game_id": restored["game_id"], "name": str(restored.get("name") or ""), "renamed": renamed}

        _, result = transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"ok": True, "restored": True, **result})

    @route("POST", "/api/v2/library/trash/purge")
    def _api_post_api_v2_library_trash_purge(self, payload):
        """Permanently drop trash entries: ``ids`` selects entries, absent purges all."""
        ids = payload.get("ids")
        wanted = None
        if ids is not None:
            if not isinstance(ids, list):
                raise BadRequest("ids must be a list of trash entry ids.")
            wanted = {str(item) for item in ids}

        def mutate(state):
            trash = state.get("trash") or []
            if wanted is None:
                state["trash"] = []
                return len(trash)
            kept = [
                item for item in trash
                if not (isinstance(item, dict) and item.get("trash_id") in wanted)
            ]
            state["trash"] = kept
            return len(trash) - len(kept)

        _, purged = transact_state(mutate)
        self.send_json(200, {"ok": True, "purged": purged})
