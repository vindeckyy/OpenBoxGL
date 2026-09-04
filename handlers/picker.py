"""PickerHandlers — "What should I play?" suggestions."""
from __future__ import annotations

from openbox import load_state_readonly
from pkg.parity.parity_picker import pick_games
from routes.registry import route

VALID_MOODS = {"any", "action", "chill", "story", "retro", "party"}
VALID_FAMILIARITIES = {"any", "new", "favorite"}
VALID_SCOPES = {"all", "platform", "playlist"}


class PickerHandlers:
    @route("POST", "/api/v2/library/pick")
    def _api_post_api_v2_library_pick(self, payload):
        from api_errors import BadRequest

        # Dispatch passes the already-parsed JSON body (web_app._do_POST reads
        # rfile exactly once); never call self.body() here — re-reading the
        # exhausted stream blocks until the socket times out.
        body = payload
        if not isinstance(body, dict):
            raise BadRequest("body must be an object")

        minutes = 0
        raw_minutes = body.get("minutes")
        if raw_minutes is not None:
            try:
                minutes = int(raw_minutes)
            except (TypeError, ValueError) as error:
                raise BadRequest("minutes must be an integer") from error
            if minutes < 0:
                raise BadRequest("minutes must be >= 0")

        mood = str(body.get("mood", "any")).lower()
        if mood not in VALID_MOODS:
            raise BadRequest(f"mood must be one of {sorted(VALID_MOODS)}")

        familiarity = str(body.get("familiarity", "any")).lower()
        if familiarity not in VALID_FAMILIARITIES:
            raise BadRequest(f"familiarity must be one of {sorted(VALID_FAMILIARITIES)}")

        players = 1
        raw_players = body.get("players")
        if raw_players is not None:
            try:
                players = int(raw_players)
            except (TypeError, ValueError) as error:
                raise BadRequest("players must be an integer") from error
            if not 1 <= players <= 8:
                raise BadRequest("players must be between 1 and 8")

        scope = str(body.get("scope", "all")).lower()
        if scope not in VALID_SCOPES:
            raise BadRequest(f"scope must be one of {sorted(VALID_SCOPES)}")
        scope_name = str(body.get("scope_name", "")).strip()

        raw_excluded = body.get("excluded_ids", [])
        if raw_excluded is None:
            raw_excluded = []
        if not isinstance(raw_excluded, list):
            raise BadRequest("excluded_ids must be an array")
        if len(raw_excluded) > 10:
            raise BadRequest("excluded_ids must contain at most 10 ids")
        excluded_ids: list = []
        for item in raw_excluded:
            if isinstance(item, bool):
                raise BadRequest("excluded_ids must be integers or strings")
            if isinstance(item, int):
                excluded_ids.append(item)
            elif isinstance(item, str) and item.strip():
                excluded_ids.append(item.strip())
            else:
                raise BadRequest("excluded_ids must be integers or strings")

        state = load_state_readonly()
        # Pass raw game dicts to pick_games (it does safe .get() with
        # defaults); skip _game_for_picker projection to avoid 10k throwaway
        # dicts. Filter hidden games early for the "all" scope.
        raw_games = state.get("games", [])
        if scope == "platform":
            games = [g for g in raw_games if isinstance(g, dict) and g.get("platform") == scope_name]
        elif scope == "playlist":
            playlists = state.get("playlists", []) if isinstance(state.get("playlists"), list) else []
            playlist = next((p for p in playlists if p.get("name") == scope_name), None)
            if playlist is None:
                raise BadRequest(f"playlist not found: {scope_name}")
            members = set(str(m) for m in playlist.get("members", []))
            games = [g for g in raw_games if isinstance(g, dict) and (str(g.get("id")) in members or str(g.get("game_id")) in members)]
        else:
            games = [g for g in raw_games if isinstance(g, dict)]

        history = state.get("history", []) if isinstance(state.get("history"), list) else []

        criteria = {
            "minutes": minutes,
            "mood": mood,
            "familiarity": familiarity,
            "players": players,
            "scope": scope,
            "scope_name": scope_name,
            "limit": 3,
            "excluded_ids": excluded_ids,
        }
        picks = pick_games(games, history, criteria)
        self.send_json(200, {"picks": picks})
        return
