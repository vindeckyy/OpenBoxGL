#!/usr/bin/env python3
"""Frontend contract: var(--*) defined, surface-deep in themes."""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "static" / "app.css"
THEMES = sorted((ROOT / "themes").glob("*.css"))

ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)
VAR_DEF_RE = re.compile(r"--([\w-]+)\s*:")
VAR_USE_RE = re.compile(r"var\(--([\w-]+)")
IGNORED_DYNAMIC = {"motion-index", "coverflow-offset"}

def parse_root_vars(css_text: str):
    m = ROOT_BLOCK_RE.search(css_text)
    if not m:
        return set()
    return set(VAR_DEF_RE.findall(m.group(0)))

def find_vars_used_outside_root(css_text: str):
    without = ROOT_BLOCK_RE.sub("", css_text)
    return set(v for v in VAR_USE_RE.findall(without) if v not in IGNORED_DYNAMIC)

def test_app_vars_defined():
    css = APP.read_text()
    defs = parse_root_vars(css)
    used = find_vars_used_outside_root(css)
    missing = sorted(used - defs)
    assert not missing, f"app.css uses vars not defined in :root: {missing}\n defined: {sorted(defs)}"

def test_themes_surface_deep():
    missing_themes = []
    for p in THEMES:
        css = p.read_text()
        defs = parse_root_vars(css)
        if "surface-deep" not in defs:
            missing_themes.append(p.name)
    assert not missing_themes, f"themes missing --surface-deep: {missing_themes}"

def test_themes_vars_defined():
    app_defs = parse_root_vars(APP.read_text())
    for p in THEMES:
        css = p.read_text()
        defs = parse_root_vars(css) | app_defs
        used = find_vars_used_outside_root(css)
        missing = sorted(used - defs)
        assert not missing, f"{p.name} uses undefined vars: {missing}"

if __name__ == "__main__":
    try:
        test_app_vars_defined()
        print("PASS test_app_vars_defined")
    except AssertionError as e:
        print(f"FAIL test_app_vars_defined: {e}")
    try:
        test_themes_surface_deep()
        print("PASS test_themes_surface_deep")
    except AssertionError as e:
        print(f"FAIL test_themes_surface_deep: {e}")
    try:
        test_themes_vars_defined()
        print("PASS test_themes_vars_defined")
    except AssertionError as e:
        print(f"FAIL test_themes_vars_defined: {e}")
    try:
        test_app_vars_defined()
        test_themes_surface_deep()
        test_themes_vars_defined()
        print("ALL PASS")
    except AssertionError:
        print("SOME FAIL")
        raise SystemExit(1) from None
