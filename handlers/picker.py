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

        # Game DNA integration (Flagship 9): additive optional params.
        # seed_query runs the query through DNA search; dna_boost (>0)
        # re-weights picks toward DNA-similar games. Defaults keep the
        # legacy behavior bit-for-bit identical.
        seed_query = str(body.get("seed_query", "") or "").strip()
        dna_boost = 0.0
        raw_dna_boost = body.get("dna_boost")
        if raw_dna_boost is not None:
            try:
                dna_boost = float(raw_dna_boost)
            except (TypeError, ValueError) as error:
                raise BadRequest("dna_boost must be a number") from error
            if dna_boost < 0:
                raise BadRequest("dna_boost must be >= 0")

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
        }
        # ``pick_games`` deliberately randomizes among the weighted top
        # candidates. Do not cache final picks: repeated requests must be able
        # to produce a new suggestion without waiting for a library mutation.
        picks = pick_games(games, history, criteria)
        if seed_query and dna_boost > 0:
            picks = _apply_dna_boost(picks, games, seed_query, dna_boost, state)
        self.send_json(200, {"picks": picks})
        return


def _apply_dna_boost(picks, games, seed_query, dna_boost, state):
    """Re-weight picks toward games similar to ``seed_query`` (DNA search).

    Additive-only: with no seed_query/dna_boost the caller never reaches
    here, so default pick behavior is unchanged.
    """
    from handlers.discovery import dna_index_for_search
    from pkg.parity import parity_dna

    settings = state.get("settings") or {}
    locale = str(settings.get("locale") or "en")[:5]
    index, _degraded, _building = dna_index_for_search(state)
    if index is None:
        return picks
    outcome = parity_dna.parse_dna_query(seed_query, games, index, locale, limit=100)
    dna_scores = {row["game_id"]: row["score"] for row in outcome.get("results", [])}
    if not dna_scores:
        return picks
    top = max(dna_scores.values()) or 1.0
    boosted = []
    for pick in picks:
        pick = dict(pick)
        dna_score = dna_scores.get(str(pick.get("game_id", "")), 0.0)
        if dna_score > 0:
            pick["score"] = round(pick.get("score", 0) * (1.0 + dna_boost * dna_score / top), 2)
            pick["dna_seed"] = True
        boosted.append(pick)
    boosted.sort(key=lambda item: (-item.get("score", 0), str(item.get("game_id", ""))))
    return boosted
