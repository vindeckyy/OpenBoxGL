#!/usr/bin/env python3
"""Enforce design-system token rule: no raw color literals outside :root.

Scans:
  - ``static/app.css`` and ``themes/*.css``: hex outside ``:root`` blocks.
  - ``index.html`` and ``static/*.js``: hex anywhere (no ``:root`` to exempt
    them; inline styles and scripted colors must use the CSS custom
    properties). Computed ``rgba(...)`` calls that derive their channels from
    tokens (e.g. ``static/mood.js``) are not raw literals and are allowed.

Counts must stay at or below the ratcheted baselines below. Decrease only.
Baselines: CSS 0 (ratcheted 2026-08-26 after F21 token-only themes), markup/JS
0 (ratcheted 2026-09-16 after the 1.13 token sweep).

Run directly: python3 scripts/check_tokens.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_BASELINE = 0
MARKUP_BASELINE = 0
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")
ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)


def count_outside_root(css_text: str) -> int:
    # Remove :root blocks to count only component rules
    without_root = ROOT_BLOCK_RE.sub("", css_text)
    return len(HEX_RE.findall(without_root))


def count_raw_hex(text: str) -> int:
    """Count every hex color literal in markup or script."""
    return len(HEX_RE.findall(text))


def _css_files() -> list[Path]:
    return [ROOT / "static" / "app.css", *sorted((ROOT / "themes").glob("*.css"))]


def _markup_files() -> list[Path]:
    return [ROOT / "index.html", *sorted((ROOT / "static").glob("*.js"))]


def _fail(message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def main() -> int:
    css_total = 0
    for path in _css_files():
        if not path.is_file():
            print(f"missing {path}", file=sys.stderr)
            return 1
        css_total += count_outside_root(path.read_text())
    print(f"raw hex outside :root (CSS): {css_total} (baseline {CSS_BASELINE})")
    if css_total > CSS_BASELINE:
        return _fail(
            f"raw hex count {css_total} > baseline {CSS_BASELINE}. "
            "Move colors to tokens in :root."
        )

    markup_total = 0
    for path in _markup_files():
        if not path.is_file():
            print(f"missing {path}", file=sys.stderr)
            return 1
        markup_total += count_raw_hex(path.read_text())
    print(f"raw hex in index.html + static/*.js: {markup_total} (baseline {MARKUP_BASELINE})")
    if markup_total > MARKUP_BASELINE:
        return _fail(
            f"raw hex count {markup_total} > baseline {MARKUP_BASELINE}. "
            "Add a token in static/app.css :root and reference it (var(--token))."
        )

    if css_total < CSS_BASELINE or markup_total < MARKUP_BASELINE:
        print("Note: count below baseline. Ratchet the baseline down in scripts/check_tokens.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
