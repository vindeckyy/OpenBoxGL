#!/usr/bin/env python3
"""Frontend API gate (ADR 0091, ADR 0103): every API URL is versioned or uses a helper.

Call sites go through ``api()``, which maps ``/api/<x>`` to ``/api/v2/<x>`` (static/state.js
and util.js ``apiPath``). A direct URL builder (an image, stylesheet, video, iframe, or
``fetch``/``EventSource``) must already name ``/api/v2`` or ``/api/host``. Comment lines are
ignored. Failures print file and line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1_PATH = re.compile(r"/api/v1\b")
DIRECT_CALL = re.compile(r"""(?:fetch|EventSource)\(\s*[`'"](/api/[^`'"]*)""")
API_LITERAL = re.compile(r"""[`'"](/api/[A-Za-z0-9_$][A-Za-z0-9_./?=&${}-]*)""")
HELPER_CALL = re.compile(r"""(?:api|apiPath)\(\s*$""")
COMMENT = re.compile(r"^\s*(?://|/\*|\*)")
VERSIONED_PREFIXES = ("/api/v2/", "/api/host/")


def frontend_files() -> list[Path]:
    files = sorted((ROOT / "static").rglob("*.js"))
    index = ROOT / "index.html"
    return files + ([index] if index.is_file() else [])


def scan_text(text: str) -> list[str]:
    """Return bare unversioned API literals that are not passed to api()/apiPath()."""
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if COMMENT.match(line):
            continue
        for match in API_LITERAL.finditer(line):
            target = match.group(1)
            if target in {"/api/v2", "/api/host"} or target.startswith(VERSIONED_PREFIXES):
                continue
            before = line[: match.start()]
            if HELPER_CALL.search(before):
                continue
            problems.append(f"line {number}: bare unversioned API URL {target}; use api() or apiPath()")
    return problems


def main() -> int:
    problems: list[str] = []
    for path in frontend_files():
        relative = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if COMMENT.match(line):
                continue
            if V1_PATH.search(line):
                problems.append(f"{relative}:{number}: /api/v1 is retired; use /api/v2")
            for match in DIRECT_CALL.finditer(line):
                target = match.group(1)
                if not target.startswith(VERSIONED_PREFIXES):
                    problems.append(f"{relative}:{number}: direct request to unversioned {target}; use api()")
        for problem in scan_text(text):
            problems.append(f"{relative}:{problem}")
    if problems:
        print("FAIL: frontend API paths drift from the v2 contract")
        for problem in problems[:80]:
            print("  " + problem)
        return 1
    print(f"frontend API paths OK: {len(frontend_files())} files use /api/v2 or the api() helper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
