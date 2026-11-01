#!/usr/bin/env python3
"""Regenerate the archived frozen v1 route list at docs/archive/v1_contracts.json.

Historical tooling: the v1 surface was retired in 2.0 (ADR 0091). The live
public contract is api_contracts.json gated by scripts/check_api_contract.py;
this script exists only to reproduce the archived file for comparison.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES  # noqa: E402
from routes.registry import _ensure_handlers_loaded  # noqa: E402


def generate_contract_data() -> dict:
    """Build the frozen v1 contracts mapping from the live route tables/registry."""
    _ensure_handlers_loaded()

    routes_by_path: dict[str, list[str]] = {}
    seen = set()
    for prefix in sorted(V1_ALIASED_PREFIXES):
        v1_path = "/api/v1" + prefix[len("/api"):]
        if v1_path in seen:
            continue
        seen.add(v1_path)
        methods = []
        if v1_path in GET_TABLE:
            methods.append("GET")
        if v1_path in POST_TABLE:
            methods.append("POST")
        routes_by_path[v1_path] = sorted(methods)

    route_entries = [
        {"methods": methods, "path": path}
        for path, methods in sorted(routes_by_path.items())
    ]
    return {
        "version": 1,
        "routes": route_entries,
    }


def main():
    target_path = ROOT / "docs" / "archive" / "v1_contracts.json"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target_path = Path(sys.argv[1]).resolve()

    data = generate_contract_data()
    content = json.dumps(data, indent=2) + "\n"
    target_path.write_text(content, encoding="utf-8")
    print(f"Wrote {len(data['routes'])} routes to {target_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
