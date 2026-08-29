"""InsightsHandlers — Play Insights dashboard (1.7.1)."""

import datetime
from urllib.parse import parse_qs

from openbox import load_state
from pkg.parity.parity_insights import compute_heatmap, summarize
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
        payload = summarize(state, end_date=end_date)
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
