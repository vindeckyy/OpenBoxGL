#!/usr/bin/env python3
"""CSP framing regression check (1.12.0).

The 1.11 security headers broke the in-app document reader by marking every
response ``frame-ancestors 'none'``. The contract since: everything stays
``'none'`` except the two document endpoints, which relax only framing to
``'self'`` via ``frameable=True``. This gate fails if:

- ``CSP_DEFAULT`` loses ``frame-ancestors 'none'``,
- ``CSP_FRAMEABLE`` drifts from ``CSP_DEFAULT`` by more than that directive,
- any ``frameable=True`` call site appears outside ``handlers/data.py``
  (the ``/api/document`` + ``/api/platform/document`` home),
- ``X-Frame-Options`` loses its DENY/SAMEORIGIN pairing.

Run directly: python3 scripts/check_csp.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv-dev", "build", "__pycache__", "node_modules", ".git"}
FRAMEABLE_HOME = Path("handlers/data.py")


def _failures() -> list[str]:
    failures = []
    source = (ROOT / "web_app.py").read_text(encoding="utf-8")

    default = re.search(r'^CSP_DEFAULT = "(.*)"$', source, re.MULTILINE)
    frameable = re.search(r'^CSP_FRAMEABLE = CSP_DEFAULT\.replace\("([^"]+)", "([^"]+)"\)$', source, re.MULTILINE)
    if not default or "frame-ancestors 'none'" not in default.group(1):
        failures.append("CSP_DEFAULT must contain frame-ancestors 'none'")
    if not frameable or frameable.groups() != ("frame-ancestors 'none'", "frame-ancestors 'self'"):
        failures.append("CSP_FRAMEABLE must relax only frame-ancestors 'none' -> 'self'")
    if '"X-Frame-Options", "SAMEORIGIN" if frameable else "DENY"' not in source:
        failures.append("X-Frame-Options DENY/SAMEORIGIN pairing is missing")

    for path in sorted(ROOT.rglob("*.py")):
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.name == "check_csp.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "frameable=True" in line and relative != FRAMEABLE_HOME:
                failures.append(f"{relative}:{lineno}: frameable=True outside {FRAMEABLE_HOME}")
    return failures


def main() -> int:
    failures = _failures()
    for failure in failures:
        print(f"CSP: {failure}")
    if not failures:
        print("CSP framing contract OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
