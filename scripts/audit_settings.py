#!/usr/bin/env python3
"""Settings retirement audit (ADR 0060, plan W1-09).

A settings key may be retired only when it has had no reader and no writer for at
least two minor releases. This script lists every key in ``settings_schema.KNOWN_SETTINGS``
with the number of source references that read or write it, outside the schema, the
frozen contract, the generated docs, and the tests. A key with zero references is a
retirement candidate; the git history decides whether it qualifies (see
docs/development/SETTINGS-AUDIT.md). Run from the repo root:

    python3 -B scripts/audit_settings.py            # human-readable table
    python3 -B scripts/audit_settings.py --json     # machine-readable
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_NAMES = {"settings_schema.py", "settings.json", "audit_settings.py"}
EXCLUDED_DIRS = {"tests", "docs", ".venv-dev", "build", ".git", "scripts/contracts"}
SOURCE_SUFFIXES = {".py", ".js", ".html", ".c", ".m", ".ps1", ".sh", ".cmd", ".yml"}


def _sources() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if path.name in EXCLUDED_NAMES or any(relative == d or relative.startswith(d + "/") for d in EXCLUDED_DIRS):
            continue
        if relative.startswith(("locales/", "themes/")):
            continue
        found.append(path)
    return found


def audit() -> list[dict[str, object]]:
    sys.path.insert(0, str(ROOT))
    from settings_schema import KNOWN_SETTINGS

    texts = {path: path.read_text(encoding="utf-8", errors="replace") for path in _sources()}
    rows = []
    for key in sorted(KNOWN_SETTINGS):
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])")
        hits = {}
        for path, text in texts.items():
            count = len(pattern.findall(text))
            if count:
                hits[path.relative_to(ROOT).as_posix()] = count
        rows.append({"key": key, "references": sum(hits.values()), "files": sorted(hits)})
    return rows


def main(argv: list[str]) -> int:
    rows = audit()
    if "--json" in argv:
        print(json.dumps(rows, indent=2))
        return 0
    candidates = [row for row in rows if row["references"] == 0]
    print(f"{len(rows)} known settings; {len(candidates)} with no references outside schema, contract, and tests")
    for row in candidates:
        print(f"  candidate: {row['key']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
