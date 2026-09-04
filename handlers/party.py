"""PartyHandlers — Game Night party queue (Big Box party mode)."""
from __future__ import annotations

from openbox import load_state
from pkg.parity.parity_party import build_party_queue
from routes.registry import route
from webapp_state import transact_state


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
        queue = build_party_queue(games, players=players, minutes=minutes)

        def mutate(live):
            settings = live.setdefault("settings", {})
            settings["party_queue"] = queue
            settings["party_players"] = players
            settings["party_index"] = 0

        transact_state(mutate)
        self.send_json(200, {"queue": queue, "count": len(queue)})
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
        index = (index + 1) % len(queue)
        game_id = queue[index]

        games = state.get("games", []) if isinstance(state, dict) else []
        name = game_id
        for game in games:
            if not isinstance(game, dict):
                continue
            if str(game.get("game_id") or "") == game_id or str(game.get("id") or "") == game_id:
                name = str(game.get("name") or game_id)
                break

        def mutate(live):
            live.setdefault("settings", {})["party_index"] = index

        transact_state(mutate)
        self.send_json(200, {"game_id": game_id, "name": name, "index": index})
        return
