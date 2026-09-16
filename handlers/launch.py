"""LaunchHandlers capability handlers. Launch Doctor preflight routes."""

import secrets

from api_errors import BadRequest
from openbox import load_state
from parity_launch_doctor import preflight_batch, preflight_single
from pkg.parity.launch_tokens import find_invalid_tokens
from routes.registry import route


def _iter_launch_templates(payload):
    """Yield launch command templates supplied in a preflight request body.

    Candidates may carry an explicit ``launch``/``launch_command`` override;
    the doctor validates the resolved argv, but unknown tokens in a caller
    supplied template must surface as a BadRequest instead of being ignored.
    """
    if not isinstance(payload, dict):
        return
    for key in ("launch", "launch_command"):
        template = payload.get(key)
        if isinstance(template, str) and template:
            yield template
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        for key in ("launch", "launch_command"):
            template = candidate.get(key)
            if isinstance(template, str) and template:
                yield template
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            yield from _iter_launch_templates(item)


def _reject_invalid_launch_tokens(payload):
    invalid = []
    for template in _iter_launch_templates(payload):
        invalid.extend(find_invalid_tokens(template))
    if invalid:
        tokens = ", ".join(sorted(set(invalid)))
        raise BadRequest(f"Launch template contains unknown tokens: {tokens}.")


class LaunchHandlers:
    @route("POST", "/api/v2/launch/preflight")
    def _api_post_api_v2_launch_preflight(self, payload):
        self.launch_preflight(payload)

    @route("POST", "/api/v2/launch/preflight/batch")
    def _api_post_api_v2_launch_preflight_batch(self, payload):
        self.launch_preflight_batch(payload)

    def launch_preflight(self, payload, *, request_id=None):
        _reject_invalid_launch_tokens(payload)
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
        _reject_invalid_launch_tokens(payload)
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
