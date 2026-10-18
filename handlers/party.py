"""PartyHandlers — Game Night party queue (Big Box party mode)."""
from __future__ import annotations

import re
import secrets

from openbox import load_state
from pkg.parity.parity_party import (
    build_party_queue,
    delete_deck,
    empty_queue_reason,
    list_decks,
    queue_exclusion_breakdown,
    save_deck,
)
from pkg.parity.parity_query import filter_games_by_query, parse_query
from routes.registry import route
from webapp_state import transact_state

SEED_RE = re.compile(r"^[0-9a-f]{8}$")


def _queue_from_settings(settings: dict) -> tuple[list[str], int, int]:
    """Return (queue, players, index) cleaned defensively for serving."""
    from handlers.settings import _clean_party

    try:
        queue, players, index = _clean_party(settings)
    except ValueError:
        return [], 2, 0
    return queue, players, index


class PartyHandlers:
    @route("POST", "/api/v2/party/queue")
    def _api_post_api_v2_party_queue(self, payload):
        from api_errors import BadRequest

        # Dispatch passes the already-parsed JSON body (web_app._do_POST reads
        # rfile exactly once); never call self.body() here — re-reading the
        # exhausted stream blocks until the socket times out.
        body = payload
        if not isinstance(body, dict):
            raise BadRequest("body must be an object")

        players = 2
        raw_players = body.get("players", 2)
        try:
            players = int(raw_players)
        except (TypeError, ValueError) as error:
            raise BadRequest("players must be an integer") from error
        if not 2 <= players <= 8:
            raise BadRequest("players must be between 2 and 8")

        minutes = 0
        raw_minutes = body.get("minutes", 0)
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError) as error:
            raise BadRequest("minutes must be an integer") from error
        if minutes < 0:
            raise BadRequest("minutes must be >= 0")

        state = load_state()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]
        raw_query = body.get("query", "")
        if not isinstance(raw_query, str):
            raise BadRequest("query must be text", code="PARTY_QUERY_INVALID")
        query = raw_query.strip()
        if len(query) > 500:
            raise BadRequest("query must be at most 500 characters", code="PARTY_QUERY_INVALID")
        if query:
            try:
                games = filter_games_by_query(games, parse_query(query))
            except (ValueError, TypeError) as error:
                raise BadRequest(f"query is invalid: {error}", code="PARTY_QUERY_INVALID") from error
        seed = body.get("seed")
        if seed in (None, ""):
            seed = secrets.token_hex(4)
        if not isinstance(seed, str) or not SEED_RE.fullmatch(seed):
            raise BadRequest("seed must be 8 lowercase hex characters", code="PARTY_SEED_INVALID")
        queue = build_party_queue(games, players=players, minutes=minutes, seed=int(seed, 16))

        def mutate(live):
            settings = live.setdefault("settings", {})
            settings["party_queue"] = queue
            settings["party_players"] = players
            settings["party_index"] = 0

        transact_state(mutate)
        breakdown = queue_exclusion_breakdown(games, players=players, minutes=minutes)
        self.send_json(200, {
            "queue": queue,
            "count": len(queue),
            "seed": seed,
            "query": query,
            "matched": len(games),
            "empty_reason": empty_queue_reason(breakdown, players) if not queue else None,
            "excluded": breakdown,
        })
        return

    @route("GET", "/api/v2/party/queue")
    def _api_get_api_v2_party_queue(self, parsed):
        state = load_state()
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        queue, _players, index = _queue_from_settings(settings if isinstance(settings, dict) else {})
        self.send_json(200, {"queue": queue, "index": index if queue else 0})
        return

    @route("POST", "/api/v2/party/next")
    def _api_post_api_v2_party_next(self, payload):
        from api_errors import BadRequest

        state = load_state()
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        queue, _players, index = _queue_from_settings(settings if isinstance(settings, dict) else {})
        if not queue:
            raise BadRequest("party queue is empty — build one first")

        # Advance from the *live* index inside the transaction: computing it
        # from the pre-read snapshot let two concurrent "next" presses both
        # land on the same game (lost update) and let a queue rebuilt in
        # between be indexed with the stale queue's position.
        def mutate(live):
            live_settings = live.setdefault("settings", {})
            live_queue, _live_players, live_index = _queue_from_settings(
                live_settings if isinstance(live_settings, dict) else {}
            )
            if not live_queue:
                return None
            live_index = (live_index + 1) % len(live_queue)
            live_settings["party_index"] = live_index
            game_id = live_queue[live_index]
            name = game_id
            games = live.get("games", [])
            for game in games if isinstance(games, list) else []:
                if not isinstance(game, dict):
                    continue
                if str(game.get("game_id") or "") == game_id or str(game.get("id") or "") == game_id:
                    name = str(game.get("name") or game_id)
                    break
            return game_id, name, live_index

        outcome = transact_state(mutate)[1]
        if outcome is None:
            raise BadRequest("party queue is empty — build one first")
        game_id, name, index = outcome
        self.send_json(200, {"game_id": game_id, "name": name, "index": index})
        return

    @route("GET", "/api/v2/party/decks")
    def _api_get_api_v2_party_decks(self, parsed):
        self.send_json(200, {"decks": list_decks(load_state())})
        return

    @route("POST", "/api/v2/party/decks")
    def _api_post_api_v2_party_decks(self, payload):
        from api_errors import BadRequest

        body = payload
        if not isinstance(body, dict):
            raise BadRequest("body must be an object")
        try:
            def mutate(state):
                return save_deck(
                    state,
                    name=body.get("name"),
                    query=body.get("query", ""),
                    players=body.get("players", 2),
                    minutes=body.get("minutes", 0),
                )

            _state, deck = transact_state(mutate)
        except ValueError as error:
            raise BadRequest(str(error), code="PARTY_DECK_INVALID") from error
        self.send_json(200, {"ok": True, "deck": deck})
        return

    @route("POST", "/api/v2/party/decks/delete")
    def _api_post_api_v2_party_decks_delete(self, payload):
        from api_errors import BadRequest, NotFound

        body = payload
        if not isinstance(body, dict):
            raise BadRequest("body must be an object")
        name = str(body.get("name") or "").strip()
        if not name:
            raise BadRequest("name is required", code="PARTY_DECK_INVALID")
        _state, deleted = transact_state(lambda state: delete_deck(state, name))
        if not deleted:
            raise NotFound("Deck not found.", code="PARTY_DECK_NOT_FOUND")
        self.send_json(200, {"ok": True, "deleted": name})
        return
