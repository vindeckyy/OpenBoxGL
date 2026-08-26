"""LaunchHandlers capability handlers. Launch Doctor preflight routes."""

import secrets

from openbox import load_state
from parity_launch_doctor import preflight_batch, preflight_single
from routes.registry import route


class LaunchHandlers:
    @route("POST", "/api/v2/launch/preflight")
    def _api_post_api_v2_launch_preflight(self, payload):
        self.launch_preflight(payload)

    @route("POST", "/api/v2/launch/preflight/batch")
    def _api_post_api_v2_launch_preflight_batch(self, payload):
        self.launch_preflight_batch(payload)

    def launch_preflight(self, payload, *, request_id=None):
        fail_on_blocked = bool(payload.get("fail_on_blocked", False))
        result = preflight_single(payload, state=load_state())
        if fail_on_blocked and result["status"] == "blocked":
            self.send_json(409, {
                "code": "LAUNCH_PREFLIGHT_BLOCKED",
                "request_id": request_id or secrets.token_hex(4),
                "status": "blocked",
                "game_id": result["game_id"],
                "candidate_id": result["candidate_id"],
                "resolved": result["resolved"],
                "checks": result["checks"],
            })
            return
        self.send_json(200, result)

    def launch_preflight_batch(self, payload, *, request_id=None):
        fail_on_blocked = bool(payload.get("fail_on_blocked", False))
        result = preflight_batch(payload, state=load_state())
        if fail_on_blocked and result["totals"]["blocked"] > 0:
            self.send_json(409, {
                "code": "LAUNCH_PREFLIGHT_BLOCKED",
                "request_id": request_id or secrets.token_hex(4),
                "status": "blocked",
                "totals": result["totals"],
                "by_platform": result["by_platform"],
                "results": result["results"],
            })
            return
        self.send_json(200, result)
