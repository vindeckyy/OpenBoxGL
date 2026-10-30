#!/usr/bin/env python3
"""Ratchet for the library state format (ADR 0089).

Compares the live schema constants with ``scripts/contracts/state.json``. A
change to ``STATE_SCHEMA_VERSION``, the envelope fields, the path modes, or the
sessions file format fails until the contract is regenerated with
``--update`` and committed with the change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "scripts" / "contracts" / "state.json"
ENVELOPE_FIELDS = ["format", "schema_version", "library_id", "created_with", "path_mode", "library_root"]


def live_contract() -> dict:
    sys.path.insert(0, str(ROOT))
    import state_store
    from updates import VERSION

    defaults = state_store.default_state()
    missing = [field for field in ENVELOPE_FIELDS if field not in defaults]
    if missing:
        raise SystemExit(f"default_state() is missing envelope fields: {', '.join(missing)}")
    return {
        "version": 1,
        "app_version": VERSION,
        "schema_version": state_store.STATE_SCHEMA_VERSION,
        "format": state_store.STATE_FORMAT,
        "envelope_fields": ENVELOPE_FIELDS,
        "path_modes": list(state_store.PATH_MODES),
        "sessions_file": {"name": state_store.SESSIONS_FILE_NAME, "format": state_store.SESSIONS_FORMAT},
    }


def main(argv: list[str]) -> int:
    live = live_contract()
    if "--update" in argv:
        CONTRACT.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {CONTRACT.relative_to(ROOT)}")
        return 0
    if not CONTRACT.is_file():
        print(f"FAIL: {CONTRACT.relative_to(ROOT)} is missing; run this script with --update.")
        return 1
    baseline = json.loads(CONTRACT.read_text(encoding="utf-8"))
    differences = sorted(key for key in set(live) | set(baseline) if live.get(key) != baseline.get(key))
    if differences:
        print("FAIL: state format drifted from scripts/contracts/state.json: " + ", ".join(differences))
        print("Regenerate with --update after a deliberate change and commit it with the change.")
        return 1
    print(f"state contract OK: schema {live['schema_version']}, format {live['format']}, app {live['app_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
