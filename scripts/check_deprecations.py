#!/usr/bin/env python3
"""Deprecation ledger gate (ADR 0099, ADR 0102, ADR 0103).

Reads the table in docs/api-deprecations.md. A row must name an owner, an announcement
version, and a removal version. A row marked Removed must not still be live; a row
marked Deprecated must not be past its removal version.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LEDGER = ROOT / "docs" / "api-deprecations.md"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
STATUS_REMOVED = "Removed"
STATUS_DEPRECATED = "Deprecated"


def _rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows[1:]


def _version(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))


def _current_version() -> str:
    source = (ROOT / "updates.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def _live_api_paths() -> set[str]:
    from routes import GET_TABLE, POST_TABLE

    return set(GET_TABLE) | set(POST_TABLE)


def main() -> int:
    if not LEDGER.is_file():
        print(f"FAIL: {LEDGER.relative_to(ROOT)} is missing")
        return 1
    problems: list[str] = []
    rows = _rows(LEDGER.read_text(encoding="utf-8"))
    if not rows:
        problems.append("ledger has no rows")
    current = _current_version()
    live = _live_api_paths()
    for cells in rows:
        surface, owner, announced, _default, removed, _sunset, status = (cells + [""] * 7)[:7]
        if not owner or not VERSION.match(announced) or not VERSION.match(removed):
            problems.append(f"{surface}: needs an owner, an announced version, and a removal version")
            continue
        if _version(removed) <= _version(announced):
            problems.append(f"{surface}: removal {removed} must come after announcement {announced}")
        if status.startswith(STATUS_REMOVED):
            if _version(removed) > _version(current):
                problems.append(f"{surface}: marked Removed but removal {removed} is after {current}")
            if surface.startswith("`/api/v1"):
                if any(path.startswith("/api/v1/") for path in live):
                    problems.append(f"{surface}: marked Removed but a /api/v1 route is still registered")
            # Unversioned /api base paths remain as private aliases used to build
            # the public /api/v2 twins; dispatch refuses them (tested separately).
            if surface.startswith("1.19-compatible backup write"):
                from pkg.parity import parity_backup
                if 1 in parity_backup.WRITABLE_BACKUP_FORMATS:
                    problems.append(f"{surface}: marked Removed but format 1 is still writable")
            if surface.startswith("Python 3.10"):
                source = (ROOT / "web_app.py").read_text(encoding="utf-8")
                if "sys.version_info < (3, 11)" not in source or "SystemExit" not in source:
                    problems.append(f"{surface}: marked Removed but web_app.py does not refuse Python older than 3.11")
            if "rollback-library" in surface:
                from pkg.state.library_cli import LIBRARY_COMMANDS
                if "--rollback-library" in LIBRARY_COMMANDS:
                    problems.append(f"{surface}: marked Removed but --rollback-library is still a library command")
            if surface.startswith("Plugin API v1"):
                from plugins import PLUGIN_API_VERSIONS
                if 1 in PLUGIN_API_VERSIONS:
                    problems.append(f"{surface}: marked Removed but plugin API 1 is still accepted")
                modules = (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines()
                if "plugin_runner.py" in modules or (ROOT / "plugin_runner.py").is_file():
                    problems.append(f"{surface}: marked Removed but plugin_runner.py is still a runtime module")
        elif status.startswith(STATUS_DEPRECATED):
            if _version(removed) <= _version(current):
                problems.append(f"{surface}: marked Deprecated but removal {removed} is not after {current}")
        else:
            problems.append(f"{surface}: status must start with Deprecated or Removed")
    if problems:
        print("FAIL: deprecation ledger incomplete")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"deprecations OK: {len(rows)} ledger rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
