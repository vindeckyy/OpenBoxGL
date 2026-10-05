#!/usr/bin/env python3
"""Enforce the design-system token rules (ADR 0004, ADR 0063).

Counts literals that should be tokens, outside :root blocks, in static/app.css and
themes/*.css: raw hex colours, raw animation/transition durations and easings, raw
z-index, raw border-radius and raw box-shadow geometry. A seventh counter covers JS:
`.showModal()` calls outside static/dialogs.js, which bypass the animated open/close
path. Each count fails if it rises above its baseline, which ratchets down as cleanup
progresses. Lower a baseline in the same commit that earns it; never raise one.

Hex baseline: 0 (ratcheted 2026-08-26 after F21 token-only themes).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINES = {
    "raw hex": 0,
    "raw duration": 0,
    "raw easing": 0,
    # z-index fell to 24 when the toast surface became a popover container: the
    # per-toast `z-index:20` was the only one a top-layer element never needed.
    # Ratcheted in the same commit that earned it (ADR 0060).
    "raw z-index": 24,
    "raw border-radius": 68,
    "raw box-shadow": 28,
    "raw showModal": 35,
}
BASELINE = BASELINES["raw hex"]
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}\b")
ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)
MOTION_DECL_RE = re.compile(r"(?:transition|animation)(?:-[a-z-]+)?\s*:[^;{}]*")
DURATION_RE = re.compile(r"(?<![\w.-])\d*\.?\d+m?s\b")
EASING_RE = re.compile(r"cubic-bezier\(|(?<![\w-])(?:ease|ease-in|ease-out|ease-in-out|linear)(?![\w-])")
Z_RE = re.compile(r"z-index\s*:\s*-?\d")
RADIUS_RE = re.compile(r"border-radius\s*:\s*(?!var\()([^;}]*\d[^;}]*)")
SHADOW_RE = re.compile(r"box-shadow\s*:\s*(?!none|var\()([^;}]*\d+px[^;}]*)")
MODAL_RE = re.compile(r"\.showModal\(")


def _outside_root(css_text: str) -> str:
    return ROOT_BLOCK_RE.sub("", css_text)


def count_outside_root(css_text: str) -> int:
    return len(HEX_RE.findall(_outside_root(css_text)))


def count_css(css_text: str) -> dict:
    css = _outside_root(css_text)
    motion = MOTION_DECL_RE.findall(css)
    radius = [m for m in RADIUS_RE.findall(css) if m.strip() not in ("999px", "50%", "0")]
    return {
        "raw hex": len(HEX_RE.findall(css)),
        "raw duration": sum(len(DURATION_RE.findall(d)) for d in motion),
        "raw easing": sum(len(EASING_RE.findall(d)) for d in motion),
        "raw z-index": len(Z_RE.findall(css)),
        "raw border-radius": len(radius),
        "raw box-shadow": len(SHADOW_RE.findall(css)),
    }


def count_showmodal(static_dir: Path) -> int:
    return sum(len(MODAL_RE.findall(p.read_text(encoding="utf-8")))
               for p in sorted(static_dir.glob("*.js")) if p.name != "dialogs.js")


def main() -> int:
    totals = dict.fromkeys(BASELINES, 0)
    for path in [ROOT / "static" / "app.css", *sorted((ROOT / "themes").glob("*.css"))]:
        if not path.is_file():
            print(f"missing {path}", file=sys.stderr)
            return 1
        for key, value in count_css(path.read_text(encoding="utf-8")).items():
            totals[key] += value
    totals["raw showModal"] = count_showmodal(ROOT / "static")
    failed = False
    for key, baseline in BASELINES.items():
        total = totals[key]
        print(f"{key} outside :root: {total} (baseline {baseline})")
        if total > baseline:
            print(f"FAIL: {key} count {total} > baseline {baseline}. Use the design tokens in :root.", file=sys.stderr)
            failed = True
        elif total < baseline:
            print(f"Note: {key} count {total} < baseline {baseline}. Ratchet BASELINES down in scripts/check_tokens.py.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
