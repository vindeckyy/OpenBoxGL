#!/usr/bin/env python3
"""Fail when flat ``parity_*`` imports come back (plan W2-04, breaking change B-08).

Every parity module is imported as ``pkg.parity.parity_*``. The flat bridge that
used to resolve ``import parity_x`` was removed in 2.0, so a flat import would
fail at runtime; this gate catches it at review time. String targets used by
``patch(...)`` and ``import_module(...)`` are checked too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {".venv-dev", "build", ".git", "node_modules", "__pycache__", "docs", ".ruff_cache"}
FLAT_IMPORT = re.compile(r"^\s*(?:from|import)\s+parity_\w+", re.M)
FLAT_TARGET = re.compile(r"(?:patch|import_module)\(\s*[\"']parity_\w+")


def main() -> int:
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDE for part in relative.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (FLAT_IMPORT, FLAT_TARGET):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{relative}:{line}")
    if offenders:
        print("FAIL: flat parity_* imports found; use pkg.parity.parity_*:")
        for offender in offenders[:50]:
            print("  " + offender)
        return 1
    print("flat imports OK: every parity module is imported as pkg.parity.*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
