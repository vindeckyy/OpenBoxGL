#!/usr/bin/env python3
"""Enforce design-system token rule: no raw hex outside :root.

Counts color literals outside :root blocks in static/app.css and themes/*.css.
Fails if count rises above baseline, which ratchets down as cleanup progresses.

Baseline: 466 (measured 2026-08-13, originally 625). Decrease only.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = 466
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}\b")
ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)

def count_outside_root(css_text: str) -> int:
    # Remove :root blocks to count only component rules
    without_root = ROOT_BLOCK_RE.sub("", css_text)
    return len(HEX_RE.findall(without_root))

def main() -> int:
    total = 0
    for path in [ROOT / "static" / "app.css", *sorted((ROOT / "themes").glob("*.css"))]:
        if not path.is_file():
            print(f"missing {path}", file=sys.stderr)
            return 1
        total += count_outside_root(path.read_text())
    print(f"raw hex outside :root: {total} (baseline {BASELINE})")
    if total > BASELINE:
        print(f"FAIL: raw hex count {total} > baseline {BASELINE}. Move colors to tokens in :root.", file=sys.stderr)
        return 1
    if total < BASELINE:
        print(f"Note: count {total} < baseline {BASELINE}. Ratchet BASELINE down in scripts/check_tokens.py.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
