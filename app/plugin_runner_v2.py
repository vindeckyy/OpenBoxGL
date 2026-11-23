#!/usr/bin/env python3
"""Plugin runner for API v2: JSON Lines in, JSON Lines out (plan W9-02, ADR 0092).

One JSON object per line on stdin, one JSON object per line on stdout.

Request:
    {"id": "r1", "type": "invoke", "hook": "before_launch", "payload": {...}}
    {"id": "r2", "type": "cancel"}

Response:
    {"id": "r1", "type": "result", "result": {...}}
    {"id": "r1", "type": "error", "error": "..."}
    {"id": "r1", "type": "cancelled"}

The plugin entry is loaded once, so a session can carry several requests (a hook call, the
service round trip, and a cancel). Output is capped at 4 MiB; the runner stops with
``PLUGIN_OUTPUT_TOO_LARGE`` rather than letting a plugin flood the host.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def _write(stream, payload: dict) -> int:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    stream.write(line)
    stream.flush()
    return len(line.encode("utf-8"))


def _load(entry: Path):
    spec = importlib.util.spec_from_file_location("openbox_plugin_v2", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    entry = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if entry is None or not entry.is_file():
        print(json.dumps({"id": "", "type": "error", "error": "Plugin entry not found."}), flush=True)
        return 1
    try:
        module = _load(entry)
    except Exception as error:  # a broken plugin must not hang the host
        print(json.dumps({"id": "", "type": "error", "error": f"Plugin failed to load: {error}"}), flush=True)
        return 1

    written = 0
    cancelled = False
    for raw in sys.stdin:
        if not raw.strip():
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            written += _write(sys.stdout, {"id": "", "type": "error", "error": "Request is not valid JSON."})
            continue
        if not isinstance(request, dict):
            written += _write(sys.stdout, {"id": "", "type": "error", "error": "Request must be an object."})
            continue
        request_id = str(request.get("id") or "")
        kind = str(request.get("type") or "")
        if kind == "cancel":
            cancelled = True
            written += _write(sys.stdout, {"id": request_id, "type": "cancelled"})
            continue
        if kind != "invoke":
            written += _write(sys.stdout, {"id": request_id, "type": "error", "error": f"Unknown request type: {kind}"})
            continue
        if cancelled:
            written += _write(sys.stdout, {"id": request_id, "type": "cancelled"})
            continue
        hook = str(request.get("hook") or "")
        payload = request.get("payload")
        function = getattr(module, hook, None)
        if not callable(function):
            written += _write(sys.stdout, {"id": request_id, "type": "error", "error": f"Plugin has no hook: {hook}"})
            continue
        try:
            result = function(payload)
        except Exception as error:
            written += _write(sys.stdout, {"id": request_id, "type": "error", "error": str(error)[:500] or "Plugin failed."})
            continue
        if not isinstance(result, dict):
            written += _write(sys.stdout, {"id": request_id, "type": "error", "error": "Plugin result must be an object."})
            continue
        written += _write(sys.stdout, {"id": request_id, "type": "result", "result": result})
        if written > MAX_OUTPUT_BYTES:
            _write(sys.stdout, {"id": request_id, "type": "error", "error": "PLUGIN_OUTPUT_TOO_LARGE"})
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
