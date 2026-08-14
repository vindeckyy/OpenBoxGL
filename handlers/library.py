"""LibraryHandlers capability handlers. Library, game CRUD, favorites, tags, queue, notifications, and health.

Method bodies reference DATA, load_state, transact_state, and other
names from the live ``web_app`` namespace. ``rebind_methods`` repoints
each function's ``__globals__`` at that namespace, so the bodies run
verbatim without circular imports or snapshotting process-global state.
"""

from handlers import rebind_methods


class LibraryHandlers:
    def _api_get_api_library(self, parsed):
        etag = public_state_etag()
        if etag and self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(304)
            self.headers_common("application/json; charset=utf-8", "private, no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            return
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

    def _api_get_api_discovery(self, parsed):
        self.send_json(200, discovery_lists(load_state_view()["games"]))
        return

    def _api_get_api_related_rich(self, parsed):
        try:
            index = int(parse_qs(parsed.query)["id"][0])
            self.send_json(200, {"items": related_with_reasons(load_state_view()["games"], index)})
        except (KeyError, IndexError, ValueError):
            raise GameNotFound("Game not found") from None
        return

    def _api_get_api_filter_presets(self, parsed):
        state = load_state()
        self.send_json(200, {
            "presets": list_presets(state),
            "bigbox_quick": bigbox_quick_presets(state),
        })
        return

    def _api_get_api_explorer_facets(self, parsed):
        field = parse_qs(parsed.query).get("field", ["genre"])[0]
        state = load_state_view()
        self.send_json(200, {"field": field, "facets": explorer_facets(state["games"], field)})
        return

    def _api_get_api_launcher_menu(self, parsed):
        payload = public_state()
        self.send_json(200, {"items": launcher_menu_items(payload["games"])})
        return

    def _api_get_api_queue(self, parsed):
        self.send_json(200, {"queue": resolve_queue(load_state_view())})
        return

    def _api_get_api_notifications(self, parsed):
        state = load_state_view()
        self.send_json(200, {"notifications": state.get("notifications", []), "unread": unread_count(state)})
        return

    def _api_get_api_tags(self, parsed):
        self.send_json(200, {"tags": tag_counts(load_state_view()["games"])})
        return

    def _api_post_api_favorite(self, payload):
        self.favorite(payload)

    def _api_post_api_game(self, payload):
        self.save_game(payload)

    def _api_post_api_game_delete(self, payload):
        self.delete_game(payload)

    def _api_post_api_games_delete_steam(self, payload):
        self.delete_steam_games(payload)

    def _api_post_api_games_bulk(self, payload):
        self.bulk_edit(payload)

    def _api_post_api_games_bulk_wizard(self, payload):
        self.bulk_wizard(payload)

    def _api_post_api_queue(self, payload):
        self.queue(payload)

    def _api_post_api_notifications(self, payload):
        self.notifications(payload)

    def _api_post_api_tags(self, payload):
        self.tags(payload)

    def _api_post_api_image_group(self, payload):
        self.save_image_group(payload)

    def _api_post_api_filter_presets(self, payload):
        self.save_filter_preset(payload)

    def _api_post_api_filter_presets_delete(self, payload):
        self.delete_filter_preset(payload)

    def _api_post_api_health(self, payload):
        self.health()

    def _api_post_api_health_dedupe(self, payload):
        self.dedupe()

    def favorite(self, payload):
        def mutate(state):
            game = game_from_payload(state, payload)
            game["favorite"] = not game.get("favorite", False)
            return game["favorite"]
        _, favorite = transact_state(mutate)
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
        _, unread = transact_state(mutate)
        state = load_state()
        self.send_json(200, {"notifications": state.get("notifications", []), "unread": unread_count(state) if action == "list" else unread})

    def tags(self, payload):
        ids = payload.get("ids")
        changes = {key: payload[key] for key in ("tags", "tags_add", "tags_remove") if key in payload}
        if not changes:
            raise ValueError("No tag changes were supplied.")
        def mutate(state):
            return bulk_update(state["games"], ids, changes)
        _, updated = transact_state(mutate)
        self.send_json(200, {"updated": updated, "tags": tag_counts(load_state()["games"])})

    def save_game(self, payload):
        source = payload.get("game", {})
        game = {key: str(source[key]).strip() for key in FIELDS if key in source}
        game["extract_archive"] = bool(source.get("extract_archive"))
        game["hidden"] = bool(source.get("hidden"))
        for field in ("broken", "portable"):
            game[field] = bool(source.get(field))
        if "disc_count" in source:
            try:
                game["disc_count"] = max(0, int(source.get("disc_count") or 0))
            except (TypeError, ValueError) as error:
                raise ValueError("Disc count must be a number.") from error
        if game.get("progress", "") not in PROGRESS:
            raise ValueError("Unknown progress value.")
        try:
            game["rating"] = float(game.get("rating") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("Rating must be a number from 0 to 5.") from error
        if not 0 <= game["rating"] <= 5:
            raise ValueError("Rating must be between 0 and 5.")
        game["applications"] = self.clean_extras(source.get("applications", []), command=True)
        game["versions"] = self.clean_extras(source.get("versions", []), command=True)
        game["documents"] = self.clean_extras(source.get("documents", []), command=False)
        save_paths = source.get("save_paths", [])
        if not isinstance(save_paths, list):
            raise ValueError("Save paths must be a list.")
        game["save_paths"] = [str(path).strip() for path in save_paths if str(path).strip()][:50]
        screenshots = source.get("screenshots", [])
        if not isinstance(screenshots, list):
            raise ValueError("Screenshots must be a list.")
        game["screenshots"] = [str(path).strip() for path in screenshots if str(path).strip()][:100]
        if "alternate_names" in source:
            names = source.get("alternate_names", [])
            if isinstance(names, str):
                game["alternate_names"] = [name.strip() for name in names.split(";") if name.strip()]
            elif isinstance(names, list):
                game["alternate_names"] = [str(name).strip() for name in names if str(name).strip()][:20]
        normalize_video_fields(game)
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
        if not game.get("name"):
            raise ValueError("Name is required.")
        if not game.get("path") or not Path(game["path"]).exists():
            raise ValueError("Path must point to an existing local file.")
        def mutate(state):
            if payload.get("id") is None and not payload.get("game_id"):
                game["added_at"] = datetime.now().isoformat(timespec="seconds")
                state["games"].append(game)
            else:
                existing = game_from_payload(state, payload)
                game["game_id"] = existing.get("game_id", game.get("game_id", ""))
                existing.update(game)
        transact_state(mutate)
        clear_file_probe_cache()
        self.send_json(200, {"ok": True})

    def bulk_edit(self, payload):
        def mutate(state):
            return bulk_update(state["games"], payload.get("ids"), payload.get("changes"))
        _, changed = transact_state(mutate)
        self.send_json(200, {"updated": changed})

    def delete_game(self, payload):
        delete_media = bool(payload.get("delete_media"))
        media_paths = []
        def mutate(state):
            game = game_from_payload(state, payload)
            if delete_media:
                media_paths.extend(game_media_paths(game))
            state["games"].remove(game)
            return game.get("name", "")
        _, removed = transact_state(mutate)
        if delete_media:
            for path in media_paths:
                try:
                    remove_file_if_safe(Path(path), DATA.parent)
                except (OSError, ValueError):
                    pass
            bump_media_epoch()
        clear_file_probe_cache()
        self.send_json(200, {"removed": removed})

    def delete_steam_games(self, payload):
        def mutate(state):
            games = state["games"]
            state["games"] = [game for game in games if str(game.get("source", "")).casefold() != "steam"]
            return len(games) - len(state["games"])
        _, removed = transact_state(mutate)
        self.send_json(200, {"removed": removed})

    @staticmethod
    def clean_extras(items, command):
        if not isinstance(items, list):
            raise ValueError("Game extras must be a list.")
        clean = []
        for item in items[:100]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            record = {"name": str(item.get("name") or Path(path).stem).strip(), "path": path}
            if command:
                record["command"] = str(item.get("command", "")).strip()
            clean.append(record)
        return clean

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
        state = load_state()
        seen, duplicates, issues = {}, [], []
        for index, game in enumerate(state["games"]):
            identity = game_identity(game)
            if identity in seen:
                duplicates.append(index)
                issues.append({"id":index, "game":game.get("name", ""), "type":"Duplicate", "detail":f"Matches {state['games'][seen[identity]].get('name', '')}"})
            else:
                seen[identity] = index
            path = Path(game.get("path", ""))
            if not game.get("path") or not path.exists():
                issues.append({"id":index, "game":game.get("name", ""), "type":"Missing game", "detail":str(path)})
            if not Path(game.get("cover", "")).is_file():
                issues.append({"id":index, "game":game.get("name", ""), "type":"Missing box front", "detail":"No local cover image"})
            for kind in ("applications", "versions", "documents"):
                for extra in game.get(kind, []):
                    if not Path(extra.get("path", "")).exists():
                        issues.append({"id":index, "game":game.get("name", ""), "type":"Missing extra", "detail":extra.get("path", "")})
            for path in game.get("save_paths", []):
                if not Path(path).exists():
                    issues.append({"id":index, "game":game.get("name", ""), "type":"Missing save path", "detail":path})
            suffix = Path(game.get("path", "")).suffix.casefold()
            if suffix in {".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".iso"} and not game.get("launch") and not state["profiles"].get(game.get("platform", "")):
                issues.append({"id":index, "game":game.get("name", ""), "type":"No emulator", "detail":game.get("platform", "Unspecified")})
        self.send_json(200, {
            "games": len(state["games"]),
            "missing": sum(issue["type"] == "Missing game" for issue in issues),
            "duplicates": len(duplicates),
            "unconfigured": sum(not game.get("path") for game in state["games"]),
            "missing_media": sum(issue["type"] == "Missing box front" for issue in issues),
            "issues":issues,
        })

    def dedupe(self):
        def mutate(state):
            seen, kept, removed = set(), [], []
            for game in state["games"]:
                identity = game_identity(game)
                if identity in seen:
                    removed.append(game.get("name", ""))
                else:
                    seen.add(identity)
                    kept.append(game)
            state["games"] = kept
            return removed
        _, removed = transact_state(mutate)
        self.send_json(200, {"removed": removed})


rebind_methods(LibraryHandlers)
