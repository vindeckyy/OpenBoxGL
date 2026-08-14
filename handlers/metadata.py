"""MetadataHandlers capability handlers. Metadata search, status, apply, sync, Steam, IGDB, and batch auto-match.

Method bodies reference DATA, load_state, transact_state, and other
names from the live ``web_app`` namespace. ``rebind_methods`` repoints
each function's ``__globals__`` at that namespace, so the bodies run
verbatim without circular imports or snapshotting process-global state.
"""

from handlers import rebind_methods


class MetadataHandlers:
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

    def _api_get_api_metadata_search(self, parsed):
        if not METADATA_DATABASE.is_file():
            self.send_json(409, {"error": "Download the LaunchBox metadata database first."})
            return
        try:
            query = parse_qs(parsed.query)
            game = game_from_query(load_state_view(), query)
            title = query.get("q", [game.get("name", "")])[0]
            results = search_games(METADATA_DATABASE, title, game.get("platform", ""))
            self.send_json(200, {"results":results})
        except (KeyError, IndexError, ValueError, sqlite3.Error) as error:
            self.send_json(400, {"error":str(error)})
        return

    def _api_get_api_metadata_igdb_search(self, parsed):
        query = parse_qs(parsed.query).get("q", [""])[0]
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        try:
            results = search_igdb_games(query, platform=platform)
        except (OSError, ValueError) as error:
            self.send_json(400, {"error": str(error)})
            return
        self.send_json(200, {"results": results})
        return

    def _api_post_api_metadata_steam(self, payload):
        self.steam_metadata(payload)

    def _api_post_api_metadata_sync(self, payload):
        self.sync_metadata()

    def _api_post_api_metadata_apply(self, payload):
        self.apply_metadata(payload)

    def _api_post_api_metadata_match(self, payload):
        self.match_metadata(payload)

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
            matched = 0
            errors = []
            for game in candidates:
                stable_id = str(game.get("game_id"))
                record = matches.get(str(game["name"]).strip())
                if not record:
                    continue
                def mutate(state, stable_id=stable_id, record=record):
                    game_from_payload(state, {"game_id": stable_id})["launchbox_db_id"] = str(record["database_id"])
                try:
                    transact_state(mutate)
                    matched += 1
                except (KeyError, ValueError) as error:
                    errors.append(f"{game.get('name', stable_id)}: {error}")
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


rebind_methods(MetadataHandlers)
