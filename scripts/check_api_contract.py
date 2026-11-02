#!/usr/bin/env python3
"""Ratchet for the public HTTP contract (ADR 0060, ADR 0091, ADR 0103).

Public operations live under /api/v2, the native host channel under /api/host. The
retired ledger records surfaces that left the contract on purpose. The baseline is
ratcheted against a git reference so deleting an entry from the JSON cannot hide a
removal.

Regenerate with ``--update`` after a deliberate change; when an operation really is
removed, pass ``--retire-missing "<reason>"`` in the same command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "api_contracts.json"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_ratchet  # noqa: E402


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


def _registry_problems() -> list[str]:
    from routes import GET_TABLE, POST_TABLE
    from routes.registry import all_routes

    problems = []
    for route in all_routes():
        table = GET_TABLE if route.method == "GET" else POST_TABLE
        if route.path in table and table[route.path] != route.spec:
            problems.append(f"SPEC MISMATCH {route.method} {route.path}: {table[route.path]} != {route.spec}")
    return problems


def _identity(entry: dict) -> str:
    return f"{entry.get('method', '')} {entry.get('path', '')}".strip()


def _retired_map(contract: dict) -> dict[str, str]:
    retired = {
        _identity(entry): str(entry.get("reason") or "")
        for entry in contract.get("retired", [])
        if isinstance(entry, dict)
    }
    # A contract written before 2.1 recorded the v1 bridge in its own section.
    for entry in (contract.get("v1_bridge") or {}).get("routes", []):
        if not isinstance(entry, dict):
            continue
        for method in entry.get("methods", []):
            retired[f"{method} {entry['path']}"] = "Retired in 2.1.0: the /api/v1 bridge was removed (ADR 0103)."
    return retired


def _retired_entries(contract: dict) -> list[dict]:
    entries = list(contract.get("retired", []))
    for entry in (contract.get("v1_bridge") or {}).get("routes", []):
        if not isinstance(entry, dict):
            continue
        for method in entry.get("methods", []):
            path = str(entry["path"])
            entries.append({
                "method": method,
                "path": path,
                "replacement": "/api/v2" + path[len("/api/v1"):],
                "retired_in": "2.1.0",
                "reason": "Retired in 2.1.0: the /api/v1 bridge was removed (ADR 0103).",
            })
    return entries


def live_contract(retired: list[dict] | None = None) -> dict[str, object]:
    from updates import VERSION

    return {
        "format": 2,
        "app_version": VERSION,
        "public": _surface("/api/v2/"),
        "host": _surface("/api/host/"),
        "retired": retired or [],
    }


def _retire_missing(live: dict, baseline: dict, reason: str) -> None:
    current = {_identity(entry) for entry in live["public"] + live["host"]}
    existing = {_identity(entry) for entry in live["retired"]}
    for section in ("public", "host"):
        for entry in baseline.get(section, []):
            identity = _identity(entry)
            if identity in current or identity in existing:
                continue
            live["retired"].append({
                "method": entry["method"],
                "path": entry["path"],
                "replacement": "",
                "retired_in": "2.1.0",
                "reason": reason,
            })
            existing.add(identity)


def _check_live_paths(live: dict) -> list[str]:
    problems = []
    for entry in live["public"]:
        if not entry["path"].startswith("/api/v2/"):
            problems.append(f"public route outside /api/v2: {entry['path']}")
    for entry in live["host"]:
        if not entry["path"].startswith("/api/host/"):
            problems.append(f"host route outside /api/host: {entry['path']}")
    for entry in live["public"] + live["host"]:
        if entry["path"].startswith("/api/v1/"):
            problems.append(f"retired v1 route is live: {entry['path']}")
    return problems


def main(argv: list[str]) -> int:
    baseline = {}
    if CONTRACT.is_file():
        baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))
    retired = _retired_entries(baseline)
    live = live_contract(retired)
    reason = ""
    if "--retire-missing" in argv:
        index = argv.index("--retire-missing")
        reason = argv[index + 1] if index + 1 < len(argv) else ""

    if "--update" in argv:
        missing = any(
            _identity(entry) not in {_identity(item) for item in live[section]}
            for section in ("public", "host")
            for entry in baseline.get(section, [])
        )
        if missing and not reason:
            print("FAIL: --update would remove a public or host operation.")
            print("Pass --retire-missing \"<reason>\" when the removal is deliberate.")
            return 1
        if missing:
            _retire_missing(live, baseline, reason)
        CONTRACT.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {CONTRACT.name}: {len(live['public'])} public, {len(live['host'])} host, "
              f"{len(live['retired'])} retired")
        return 0

    problems = _registry_problems()
    problems.extend(_check_live_paths(live))
    if not CONTRACT.is_file():
        print(f"FAIL: {CONTRACT.name} is missing; run this script with --update.")
        return 1
    for key in ("public", "host"):
        added = [op for op in live[key] if op not in baseline.get(key, [])]
        removed = [op for op in baseline.get(key, []) if op not in live[key]]
        problems.extend(f"{key} +{op['method']} {op['path']}" for op in added)
        problems.extend(f"{key} -{op['method']} {op['path']}" for op in removed)
    live_retired = _retired_map(live)
    problems.extend(contract_ratchet.ledger_consistency(
        [_identity(entry) for entry in live["public"] + live["host"]], live_retired
    ))
    reference, reference_label = contract_ratchet.reference_data_path("api_contracts.json")
    if reference is None:
        print(f"api contract: layer 2 skipped - {reference_label}")
    else:
        ref_public = [_identity(entry) for entry in reference.get("public", [])]
        ref_host = [_identity(entry) for entry in reference.get("host", [])]
        ref_retired = _retired_map(reference)
        problems.extend(contract_ratchet.ledger_failures(
            ref_public, ref_retired, [_identity(entry) for entry in live["public"]], live_retired,
            noun="operation",
            drop_reason="was dropped from api_contracts.json and is not in the retired ledger;",
            drop_remedy='add it to the retired list with a reason',
        ))
        problems.extend(contract_ratchet.ledger_failures(
            ref_host, ref_retired, [_identity(entry) for entry in live["host"]], live_retired,
            noun="host operation",
            drop_reason="was dropped from api_contracts.json and is not in the retired ledger;",
            drop_remedy='add it to the retired list with a reason',
        ))
    if problems:
        print("FAIL: HTTP contract drifted from api_contracts.json")
        for line in problems:
            print("  " + line)
        print("Regenerate with --update after a deliberate change and commit it with the change.")
        return 1
    print(f"api contract OK: {len(live['public'])} public, {len(live['host'])} host, "
          f"{len(live['retired'])} retired ({reference_label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
