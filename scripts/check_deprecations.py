#!/usr/bin/env python3
"""Deprecation ledger gate (ADR 0099): every deprecated surface has an owner, a version, and a removal.

Reads the table in docs/api-deprecations.md. A row must name an owner, an announcement
version, and a removal version. Removal must come after the announcement. The /api/v1
sunset date in the ledger must equal ``routes.V1_SUNSET``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "api-deprecations.md"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows[1:]  # drop the header row


def main() -> int:
    if not LEDGER.is_file():
        print(f"FAIL: {LEDGER.relative_to(ROOT)} is missing")
        return 1
    problems: list[str] = []
    rows = _rows(LEDGER.read_text(encoding="utf-8"))
    if not rows:
        problems.append("ledger has no rows")
    for cells in rows:
        surface, owner, announced, _default, removed, sunset, status = (cells + [""] * 7)[:7]
        if not owner or not VERSION.match(announced) or not VERSION.match(removed):
            problems.append(f"{surface}: needs an owner, an announced version, and a removal version")
            continue
        if tuple(map(int, removed.split("."))) <= tuple(map(int, announced.split("."))):
            problems.append(f"{surface}: removal {removed} must come after announcement {announced}")
        if surface.startswith("`/api/v1") and not status:
            problems.append(f"{surface}: status is required")
        if surface.startswith("`/api/v1"):
            sys.path.insert(0, str(ROOT))
            from routes import V1_SUNSET

            if V1_SUNSET not in sunset:
                problems.append(f"{surface}: sunset header {sunset!r} does not match routes.V1_SUNSET")
    if problems:
        print("FAIL: deprecation ledger incomplete")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"deprecations OK: {len(rows)} ledger rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
