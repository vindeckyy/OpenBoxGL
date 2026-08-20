"""MetadataHandlers capability handlers. Metadata search, status, apply, sync, Steam, IGDB, and batch auto-match."""

import copy
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import parse_qs
from api_errors import BadRequest, Conflict
from metadata import apply_game_metadata, batch_match, search_games, sync_database
from openbox import load_state
from parity_igdb import apply_to_game as apply_igdb_metadata, fetch_game as fetch_igdb_game, search_games as search_igdb_games
from routes.registry import route
from webapp_state import DATA, JOB_MANAGER, METADATA_DATABASE, METADATA_JOB, MEDIA_TYPES_ALL, PROCESS_LOCK, bump_media_epoch, game_from_payload, game_from_query, load_state_view, transact_state, update_steam_metadata


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
            raise ValueError("Download the metadata database first.")
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
                record = matches.get(str(game["name"]).strip())
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
        state = load_state()
        original_game = game_from_payload(state, payload)
        if "manual" in media_types and not str(original_game.get("path") or "").strip():
            raise ValueError("This game has no file path, so no manual can be imported.")
        stable_game_id = original_game.get("game_id")
        original = dict(original_game)
        updated = apply_game_metadata(
            dict(original), METADATA_DATABASE, int(payload["database_id"]), media_types,
            DATA.parent / "media/launchbox", bool(payload.get("overwrite")),
            region_priority=load_state().get("settings", {}).get("region_priority"),
        )
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


