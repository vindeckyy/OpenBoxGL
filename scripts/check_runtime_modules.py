#!/usr/bin/env python3
"""Verify runtime_modules.txt matches the repository layout.

Checks:
  1. Every line in runtime_modules.txt points to an existing file.
  2. No duplicate lines.
  3. Every file that must be listed is listed (handlers, pkg/state, pkg/parity, routes).
  4. Every listed file that looks generated still exists.

Fails the gate on drift so the AppImage never ships with a stale manifest.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "runtime_modules.txt"


def _read_manifest() -> list[str]:
    if not MANIFEST.is_file():
        print(f"missing {MANIFEST}", file=sys.stderr)
        sys.exit(1)
    lines = [line.strip() for line in MANIFEST.read_text().splitlines() if line.strip()]
    return lines


def main() -> int:
    lines = _read_manifest()
    # 1. duplicates
    seen = set()
    dups = []
    for line in lines:
        if line in seen:
            dups.append(line)
        seen.add(line)
    if dups:
        print(f"FAIL: duplicate entries in runtime_modules.txt: {dups}", file=sys.stderr)
        return 1

    # 2. existence
    missing = [line for line in lines if not (ROOT / line).exists()]
    if missing:
        print(f"FAIL: manifest lists missing files: {missing}", file=sys.stderr)
        return 1

    # 3. required globs must be present in manifest
    required_patterns = [
        "handlers/*.py",
        "pkg/state/*.py",
        "pkg/parity/*.py",
        "routes/*.py",
        "routes/**/*.py",
    ]
    manifest_set = set(lines)
    expected_missing = []
    for pattern in required_patterns:
        for path in ROOT.glob(pattern):
            if path.is_file():
                # Skip __pycache__ already excluded by glob
                rel = str(path.relative_to(ROOT))
                if rel not in manifest_set:
                    expected_missing.append(rel)
    if expected_missing:
        print(
            "FAIL: runtime_modules.txt missing entries for required modules:\n  "
            + "\n  ".join(sorted(set(expected_missing)))
            + "\nAdd them to runtime_modules.txt (or update this check if intentionally omitted).",
            file=sys.stderr,
        )
        return 1

    # 4. optional: warn if manifest contains entries not matching any known pattern but file exists — allowed (e.g. openbox-release.pub)
    print(f"runtime_modules.txt OK: {len(lines)} entries, all exist, required globs covered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
