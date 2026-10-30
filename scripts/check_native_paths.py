#!/usr/bin/env python3
"""Native host gate (ADR 0091): the three native hosts call only the /api/host channel.

``native_host.c``, ``native_host_win.c``, and ``native_host_mac.m`` must not name
the retired unversioned host paths or any /api/v1 route. A host may name
``/api/host/*`` and the public ``/api/v2/*`` operations only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("native_host.c", "native_host_win.c", "native_host_mac.m")
RETIRED = ("/api/launch", "/api/shutdown", "/api/native/capabilities", "/api/v2/resume")
RETIRED_PATTERN = re.compile("|".join(re.escape(path) + r"(?![\w/-])" for path in RETIRED))
V1_PATTERN = re.compile(r"/api/v1\b")


def main() -> int:
    problems: list[str] = []
    for name in HOSTS:
        path = ROOT / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            for match in RETIRED_PATTERN.finditer(line):
                problems.append(f"{name}:{number}: retired host path {match.group(0)}; use /api/host/...")
            if V1_PATTERN.search(line):
                problems.append(f"{name}:{number}: /api/v1 is retired")
    if problems:
        print("FAIL: native hosts still name retired paths")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"native paths OK: {len(HOSTS)} hosts use the /api/host channel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
