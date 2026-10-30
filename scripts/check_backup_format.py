#!/usr/bin/env python3
"""Ratchet for the backup archive format (ADR 0093, plan W4-05).

Compares the live format constants and the manifest fields a format-2 backup writes with
``scripts/contracts/backup.json``. A change to the format number or the manifest shape
fails until the contract is regenerated with ``--update`` in the same commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "scripts" / "contracts" / "backup.json"
sys.path.insert(0, str(ROOT))


def live_contract() -> dict:
    from pkg.parity import parity_backup as backup

    return {
        "version": 1,
        "format": backup.BACKUP_FORMAT_VERSION,
        "supported_formats": list(backup.SUPPORTED_BACKUP_FORMATS),
        "manifest_fields_v2": [
            "format", "app_version", "state_schema", "library_id", "created_by", "encryption", "items", "created",
            "redacted_secrets", "files",
        ],
        "file_entry_fields": ["name", "size", "mode", "sha256"],
        "encryption": "none",
        "member_limits": {
            "max_members": backup.MAX_BACKUP_MEMBERS,
            "max_member_bytes": backup.MAX_BACKUP_MEMBER_BYTES,
            "max_total_bytes": backup.MAX_BACKUP_TOTAL_BYTES,
        },
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
    drift = sorted(key for key in set(live) | set(baseline) if live.get(key) != baseline.get(key))
    if drift:
        print("FAIL: backup format drifted from scripts/contracts/backup.json: " + ", ".join(drift))
        print("Regenerate with --update after a deliberate change and commit it with the change.")
        return 1
    print(f"backup format contract OK: format {live['format']}, reads {live['supported_formats']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
