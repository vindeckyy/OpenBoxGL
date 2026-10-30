#!/usr/bin/env python3
"""Frontend API gate (ADR 0091): no /api/v1 in the UI, and no direct unversioned fetches.

Call sites go through ``api()``, which maps ``/api/<x>`` to ``/api/v2/<x>`` (static/state.js
and util.js ``apiPath``). A direct ``fetch`` or ``EventSource`` must already name
``/api/v2`` or ``/api/host``. Comment lines are ignored. Failures print file and line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1_PATH = re.compile(r"/api/v1\b")
DIRECT_CALL = re.compile(r"""(?:fetch|EventSource)\(\s*[`'"](/api/[^`'"]*)""")
COMMENT = re.compile(r"^\s*(?://|/\*|\*)")


def frontend_files() -> list[Path]:
    files = sorted((ROOT / "static").rglob("*.js"))
    index = ROOT / "index.html"
    return files + ([index] if index.is_file() else [])


def main() -> int:
    problems: list[str] = []
    for path in frontend_files():
        relative = path.relative_to(ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if COMMENT.match(line):
                continue
            if V1_PATH.search(line):
                problems.append(f"{relative}:{number}: /api/v1 is retired; use /api/v2")
            for match in DIRECT_CALL.finditer(line):
                target = match.group(1)
                if not target.startswith(("/api/v2/", "/api/host/")):
                    problems.append(f"{relative}:{number}: direct request to unversioned {target}; use api()")
    if problems:
        print("FAIL: frontend API paths drift from the v2 contract")
        for problem in problems[:80]:
            print("  " + problem)
        return 1
    print(f"frontend API paths OK: {len(frontend_files())} files use /api/v2 or the api() helper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
