"""ConstellationHandlers — library relationship graph."""
from __future__ import annotations

from openbox import load_state
from pkg.parity.parity_constellation import KINDS, build_graph
from routes.registry import route


class ConstellationHandlers:
    @route("GET", "/api/v2/library/constellation")
    def _api_get_api_v2_library_constellation(self, parsed):
        from api_errors import BadRequest

        state = load_state()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]

        qs = (getattr(parsed, "query", "") or "")
        from urllib.parse import parse_qs

        params = parse_qs(qs)

        raw_kinds = params.get("kinds", [""])[0].strip() if "kinds" in params else ""
        if raw_kinds:
            kinds = {k.strip().lower() for k in raw_kinds.split(",") if k.strip()}
            invalid = kinds - set(KINDS)
            if invalid:
                raise BadRequest(f"unknown kinds: {sorted(invalid)}; valid: {sorted(KINDS)}")
        else:
            kinds = None

        limit = 400
        raw_limit = params.get("limit", [""])[0].strip() if "limit" in params else ""
        if raw_limit:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError) as error:
                raise BadRequest("limit must be an integer") from error
            if not 50 <= limit <= 1000:
                raise BadRequest("limit must be between 50 and 1000")

        history = state.get("history", []) if isinstance(state.get("history"), list) else []
        result = build_graph(games, history, kinds=kinds, limit=limit)
        self.send_json(200, result)
        return
