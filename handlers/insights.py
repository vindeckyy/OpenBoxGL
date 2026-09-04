"""InsightsHandlers — Play Insights dashboard (1.7.1)."""

import datetime
from pathlib import Path
from urllib.parse import parse_qs

from openbox import load_state
from pkg.parity.parity_insights import compute_heatmap, mastery_summary, summarize, wrapped_summary
from routes.registry import route


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
            cache = state.get("settings", {}).get("state_dir")
            if cache:
                ra_dir = str(Path(cache) / "retroachievements")
        self.send_json(200, mastery_summary(games, ra_cache_dir=ra_dir))
        return
