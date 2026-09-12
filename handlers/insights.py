"""InsightsHandlers — Play Insights dashboard (1.7.1)."""

import datetime
from urllib.parse import parse_qs

from openbox import DATA, load_state, update_state_with_result
from pkg.parity import parity_radio
from pkg.parity.parity_insights import compute_heatmap, mastery_summary, summarize, wrapped_summary
from pkg.parity.parity_trophies import record_new_awards, trophy_case, unrecorded_awards
from routes.registry import route
from webapp_state import transact_state


class InsightsHandlers:
    @route("GET", "/api/v2/insights/summary")
    def _api_get_api_v2_insights_summary(self, parsed):
        state = load_state()
        qs = parse_qs(parsed.query or "")
        end_date = None
        raw_end = qs.get("end_date", [""])[0].strip() if qs.get("end_date") else ""
        if raw_end:
            try:
                end_date = datetime.date.fromisoformat(raw_end)
            except ValueError as error:
                from api_errors import BadRequest

                raise BadRequest("end_date must be YYYY-MM-DD") from error
        days_raw = qs.get("days", [""])[0].strip() if qs.get("days") else ""
        days = 366
        if days_raw:
            try:
                days = int(days_raw)
            except (TypeError, ValueError) as error:
                from api_errors import BadRequest

                raise BadRequest("days must be an integer") from error
            if not 1 <= days <= 366:
                from api_errors import BadRequest

                raise BadRequest("days must be between 1 and 366")
        payload = summarize(state, end_date=end_date, days=days)
        self.send_json(200, payload)
        return

    @route("GET", "/api/v2/insights/heatmap")
    def _api_get_api_v2_insights_heatmap(self, parsed):
        state = load_state()
        qs = parse_qs(parsed.query or "")
        raw_end = qs.get("end_date", [""])[0].strip() if qs.get("end_date") else ""
        end_date = None
        if raw_end:
            try:
                end_date = datetime.date.fromisoformat(raw_end)
            except ValueError as error:
                from api_errors import BadRequest

                raise BadRequest("end_date must be YYYY-MM-DD") from error
        days_raw = qs.get("days", [""])[0].strip() if qs.get("days") else ""
        days = 366
        if days_raw:
            try:
                days = int(days_raw)
            except (TypeError, ValueError) as error:
                from api_errors import BadRequest

                raise BadRequest("days must be an integer") from error
            if not 1 <= days <= 366:
                from api_errors import BadRequest

                raise BadRequest("days must be between 1 and 366")
        history = state.get("history", []) if isinstance(state, dict) else []
        if not isinstance(history, list):
            history = []
        heatmap = compute_heatmap(history, days=days, end_date=end_date)
        self.send_json(200, {"heatmap": heatmap, "days": days})
        return

    @route("GET", "/api/v2/insights/wrapped")
    def _api_get_api_v2_insights_wrapped(self, parsed):
        from api_errors import BadRequest

        qs = parse_qs(parsed.query or "")
        year_raw = qs.get("year", [""])[0].strip() if qs.get("year") else ""
        if not year_raw:
            raise BadRequest("year is required")
        try:
            year = int(year_raw)
        except (TypeError, ValueError) as error:
            raise BadRequest("year must be an integer") from error
        if not 1970 <= year <= 2100:
            raise BadRequest("year must be between 1970 and 2100")
        state = load_state()
        self.send_json(200, wrapped_summary(state, year))
        return

    @route("GET", "/api/v2/insights/mastery")
    def _api_get_api_v2_insights_mastery(self, parsed):
        state = load_state()
        games = state.get("games", []) if isinstance(state, dict) else []
        ra_dir = None
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        if settings.get("retroachievements_enabled"):
            ra_cache = DATA.parent / "cache/retroachievements"
            if ra_cache.is_dir():
                ra_dir = str(ra_cache)
        self.send_json(200, mastery_summary(games, ra_cache_dir=ra_dir))
        return

    @route("GET", "/api/v2/insights/radio")
    def _api_get_api_v2_insights_radio(self, parsed):
        # Read path first: a fresh managed playlist answers without a write.
        # Missing/stale/hollowed playlists regenerate inside the transaction so
        # a concurrent refresh is reused rather than overwritten.
        state = load_state()
        entry = parity_radio.managed_playlist(state)
        if entry is not None and not parity_radio.playlist_stale(entry):
            payload = parity_radio.entry_payload(entry, state)
            if payload["picks"] or not state.get("games"):
                self.send_json(200, {"playlist": payload})
                return
        _, payload = transact_state(lambda current: parity_radio.radio_payload(current))
        self.send_json(200, {"playlist": payload})
        return

    @route("POST", "/api/v2/insights/radio/refresh")
    def _api_post_api_v2_insights_radio_refresh(self, payload):
        _, result = transact_state(lambda current: parity_radio.radio_payload(current, force=True))
        self.send_json(200, {"playlist": result})
        return

    @route("GET", "/api/v2/insights/radar")
    def _api_get_api_v2_insights_radar(self, parsed):
        state = load_state()
        games = state.get("games", []) if isinstance(state, dict) else []
        history = state.get("history", []) if isinstance(state, dict) else []
        if not isinstance(games, list):
            games = []
        if not isinstance(history, list):
            history = []
        self.send_json(200, {"radar": parity_radio.radar(games, history, parity_radio.parked_ids(state))})
        return

    @route("POST", "/api/v2/insights/radar/park")
    def _api_post_api_v2_insights_radar_park(self, payload):
        from api_errors import BadRequest, GameNotFound

        if not isinstance(payload, dict):
            raise BadRequest("body must be an object")
        game_id = str(payload.get("game_id") or "").strip()
        if not game_id:
            raise BadRequest("game_id is required")
        _, result = transact_state(lambda current: parity_radio.park_game(current, game_id))
        if result is None:
            raise GameNotFound("Game not found")
        self.send_json(200, {
            "parked": bool(result.get("ok")),
            "game_id": result.get("game_id"),
            "name": result.get("name"),
            "progress": result.get("progress"),
            "code": result.get("code"),
        })
        return

    # ── Trophies (S6): deterministic launcher-level achievements ─────────
    # GET reads the live rule status merged with stored earn dates; POST
    # evaluates inside one state transaction, persists new awards, and
    # broadcasts them so every open client can toast once.
    @route("GET", "/api/v2/insights/trophies")
    def _api_get_api_v2_insights_trophies(self, parsed):
        self.send_json(200, trophy_case(load_state()))
        return

    @route("POST", "/api/v2/insights/trophies/evaluate")
    def _api_post_api_v2_insights_trophies_evaluate(self, payload):
        state = load_state()
        if not unrecorded_awards(state):
            case = trophy_case(state)
            self.send_json(200, {"newly_awarded": [], "earned": case["earned"], "total": case["total"]})
            return
        committed, newly = update_state_with_result(record_new_awards)
        if newly:
            from pkg.state.sse import broadcast_event

            for award in newly:
                broadcast_event("trophy.awarded", award)
        case = trophy_case(committed)
        self.send_json(200, {"newly_awarded": newly, "earned": case["earned"], "total": case["total"]})
        return
