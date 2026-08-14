#!/usr/bin/env python3
"""Fail when the live v1 route surface drifts from v1_contracts.json.

The v1 surface is the native host's only contract. A route that appears,
disappears, or changes its methods must be reflected in v1_contracts.json in
the same PR. Run from the repo root:

  python3 scripts/check_v1_contract.py

Exits 0 when the live tables match the frozen contract, 1 otherwise.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES  # noqa: E402


def live_routes():
    routes = {}
    seen = set()
    for path in sorted(V1_ALIASED_PREFIXES):
        v1 = f"/api/v1{path[len('/api'):]}"
        if v1 in seen:
            continue
        seen.add(v1)
        methods = []
        if v1 in GET_TABLE:
            methods.append("GET")
        if v1 in POST_TABLE:
            methods.append("POST")
        routes[v1] = sorted(methods)
    return routes


def main():
    contract_path = ROOT / "v1_contracts.json"
    if not contract_path.is_file():
        print(f"FAIL: {contract_path} is missing. Generate it with: "
              "python3 scripts/gen_api_docs.py --out /dev/null is not enough; "
              "run scripts/check_v1_contract.py to see the drift.")
        return 1

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    frozen = {entry["path"]: sorted(entry["methods"]) for entry in contract.get("routes", [])}
    live = live_routes()

    problems = []
    for path in sorted(set(frozen) | set(live)):
        if path not in frozen:
            problems.append(f"ADDED   {path} {live[path]}")
        elif path not in live:
            problems.append(f"REMOVED {path} {frozen[path]}")
        elif frozen[path] != live[path]:
            problems.append(f"CHANGED {path} {frozen[path]} -> {live[path]}")

    if problems:
        print("FAIL: v1 route surface drifted from v1_contracts.json")
        for line in problems:
            print("  " + line)
        print("Update v1_contracts.json in the same PR that changes the surface.")
        return 1

    print(f"v1 contract OK: {len(live)} routes match v1_contracts.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
