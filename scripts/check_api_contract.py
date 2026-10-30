#!/usr/bin/env python3
"""Ratchet for the HTTP contract (ADR 0091): public /api/v2, native host /api/host, and the retired v1 bridge.

Fails when the live surface drifts from ``api_contracts.json``. Regenerate with
``--update`` only for a deliberate change, and commit the diff with the change.
The v1 entries describe what the opt-in bridge (OPENBOX_ENABLE_V1_API=1) serves
through 2.0.x; they are removed in 2.1.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "api_contracts.json"
sys.path.insert(0, str(ROOT))


def _table_methods(path: str) -> list[str]:
    from routes import GET_TABLE, POST_TABLE

    methods = []
    if path in GET_TABLE:
        methods.append("GET")
    if path in POST_TABLE:
        methods.append("POST")
    return methods


def _surface(prefix: str) -> list[dict[str, str]]:
    from routes import GET_TABLE, POST_TABLE
    from routes.registry import all_routes

    operations: set[tuple[str, str]] = set()
    for table, method in ((GET_TABLE, "GET"), (POST_TABLE, "POST")):
        operations.update((method, path) for path in table if path.startswith(prefix))
    for route in all_routes():
        if route.path.startswith(prefix):
            operations.add((route.method, route.path))
    return [{"method": method, "path": path} for method, path in sorted(operations)]


def _v1_bridge() -> list[dict[str, object]]:
    from routes import V1_ALIASED_PREFIXES

    entries = []
    for base in sorted(V1_ALIASED_PREFIXES):
        methods = _table_methods(base)
        if methods:
            entries.append({"path": "/api/v1" + base[len("/api"):], "methods": methods})
    return entries


def _registry_problems() -> list[str]:
    from routes import GET_TABLE, POST_TABLE
    from routes.registry import all_routes

    problems = []
    for route in all_routes():
        table = GET_TABLE if route.method == "GET" else POST_TABLE
        # Decorator-only routes are dispatched through the registry; only a table entry must agree.
        if route.path in table and table[route.path] != route.spec:
            problems.append(f"SPEC MISMATCH {route.method} {route.path}: {table[route.path]} != {route.spec}")
    return problems


def live_contract() -> dict[str, object]:
    from updates import VERSION

    return {
        "format": 1,
        "app_version": VERSION,
        "public": _surface("/api/v2/"),
        "host": _surface("/api/host/"),
        "v1_bridge": {
            "enabled_by_default": False,
            "env": "OPENBOX_ENABLE_V1_API",
            "removal": "2.1.0",
            "routes": _v1_bridge(),
        },
    }


def main(argv: list[str]) -> int:
    live = live_contract()
    if "--update" in argv:
        CONTRACT.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {CONTRACT.name}: {len(live['public'])} public, {len(live['host'])} host operations")
        return 0
    problems = _registry_problems()
    if not CONTRACT.is_file():
        print(f"FAIL: {CONTRACT.name} is missing; run this script with --update.")
        return 1
    baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for key in ("public", "host"):
        added = [op for op in live[key] if op not in baseline.get(key, [])]
        removed = [op for op in baseline.get(key, []) if op not in live[key]]
        problems.extend(f"{key} +{op['method']} {op['path']}" for op in added)
        problems.extend(f"{key} -{op['method']} {op['path']}" for op in removed)
    if live["v1_bridge"] != baseline.get("v1_bridge"):
        problems.append("v1 bridge surface changed")
    if problems:
        print("FAIL: HTTP contract drifted from api_contracts.json")
        for line in problems:
            print("  " + line)
        print("Regenerate with --update after a deliberate change and commit it with the change.")
        return 1
    print(f"api contract OK: {len(live['public'])} public, {len(live['host'])} host, "
          f"{len(live['v1_bridge']['routes'])} retired v1 bridge routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
