"""PickerHandlers — "What should I play?" suggestions."""
from __future__ import annotations

import json

from openbox import load_state
from pkg.parity.parity_picker import pick_games
from routes.registry import route

VALID_MOODS = {"any", "action", "chill", "story", "retro", "party"}
VALID_FAMILIARITIES = {"any", "new", "favorite"}
VALID_SCOPES = {"all", "platform", "playlist"}


def _game_for_picker(game):
    """Project only the fields the picker needs, with safe defaults."""
    return {
        "id": game.get("id"),
        "game_id": game.get("game_id") or str(game.get("id", "")),
        "name": game.get("name", ""),
        "has_cover": game.get("has_cover", False),
        "cover": game.get("cover", ""),
        "platform": game.get("platform", ""),
        "genre": game.get("genre", ""),
        "year": game.get("year", ""),
        "favorite": game.get("favorite", False),
        "rating": game.get("rating", 0),
        "progress": game.get("progress", ""),
        "play_count": game.get("play_count", 0),
        "playtime_seconds": game.get("playtime_seconds", 0),
        "last_played": game.get("last_played", ""),
        "path_exists": game.get("path_exists", True),
        "hidden": game.get("hidden", False),
        "hide_in_bigbox": game.get("hide_in_bigbox", False),
        "store_installed": game.get("store_installed", False),
        "max_players": game.get("max_players", 1),
    }


class PickerHandlers:
    @route("POST", "/api/v2/library/pick")
    def _api_post_api_v2_library_pick(self, parsed):
        from api_errors import BadRequest

        try:
            body = self.body()
        except (json.JSONDecodeError, ValueError) as error:
            raise BadRequest("invalid JSON body") from error
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

        state = load_state()
        games = [_game_for_picker(g) for g in state.get("games", []) if isinstance(g, dict)]
        if scope == "platform":
            games = [g for g in games if g.get("platform") == scope_name]
        elif scope == "playlist":
            playlists = state.get("playlists", []) if isinstance(state.get("playlists"), list) else []
            playlist = next((p for p in playlists if p.get("name") == scope_name), None)
            if playlist is None:
                raise BadRequest(f"playlist not found: {scope_name}")
            members = set(str(m) for m in playlist.get("members", []))
            games = [g for g in games if str(g.get("id")) in members or str(g.get("game_id")) in members]

        history = state.get("history", []) if isinstance(state.get("history"), list) else []

        criteria = {
            "minutes": minutes,
            "mood": mood,
            "familiarity": familiarity,
            "players": players,
            "scope": scope,
            "scope_name": scope_name,
            "limit": 3,
        }
        picks = pick_games(games, history, criteria)
        self.send_json(200, {"picks": picks})
        return
