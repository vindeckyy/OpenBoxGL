#!/usr/bin/env python3
"""Verify i18n key coverage across all shipped locale files.

Scans index.html for data-i18n attributes and static/*.js for t('key'),
t("key"), and static-backtick t(`key`) calls. Loads every locales/*.json.
Fails if:
  - any key referenced in code is missing from en.json
  - any key in en.json is missing from a shipped locale JSON
  - any locale JSON has extra keys not in en.json
Reports coverage percentage per locale.

Unused keys and dynamic template-literal keys (t(`prefix.${expr}.suffix`),
which cannot be statically checked) are reported as informational warnings and
never fail the gate — they are visibility for the ratchet-down i18n sweep.

Run directly: python3 scripts/check_i18n.py
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "locales"
INDEX_HTML = ROOT / "index.html"
STATIC_DIR = ROOT / "static"

# Attributes that carry i18n keys
I18N_ATTRS = ("data-i18n", "data-i18n-placeholder", "data-i18n-title", "data-i18n-aria-label")

# Regex for t('key'), t("key"), and static backtick t(`key`) calls in JS.
# The matching closing quote is required so a t('...') inside a comment or a
# mismatched literal does not get counted.
T_CALL_RE = re.compile(r"""\bt\(\s*(?P<quote>['"`])(?P<key>[a-zA-Z0-9_.]+)(?P=quote)""")

# Template literals with interpolation: t(`trophy.rules.${id}.name`). The full
# key is dynamic, so only the literal prefix is statically checkable.
DYNAMIC_T_CALL_RE = re.compile(r"""\bt\(\s*`(?P<template>[^`]*)`""")


def extract_js_keys_from_text(text):
    """Static keys from t() calls in one JS source string."""
    return {match.group("key") for match in T_CALL_RE.finditer(text)}


def extract_dynamic_prefixes_from_text(text):
    """Literal prefixes of interpolated t(`...${x}...`) calls."""
    prefixes = set()
    for match in DYNAMIC_T_CALL_RE.finditer(text):
        template = match.group("template")
        if "${" in template:
            prefix = template.split("${", 1)[0].strip(".")
            if prefix:
                prefixes.add(prefix)
    return prefixes


def _flatten_keys(obj, prefix=""):
    """Flatten nested dict keys into dot-separated paths."""
    keys = set()
    if not isinstance(obj, dict):
        return keys
    for k, v in obj.items():
        full = f"{prefix}.{k}" if prefix else k
        if k == "meta":
            continue  # meta is metadata, not a translatable key
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


def _extract_html_keys():
    """Extract all data-i18n* attribute values from index.html."""
    keys = set()
    if not INDEX_HTML.exists():
        return keys
    text = INDEX_HTML.read_text(encoding="utf-8")
    for attr in I18N_ATTRS:
        pattern = re.compile(rf'{attr}="([a-zA-Z0-9_.]+)"')
        keys |= set(pattern.findall(text))
    return keys


def _extract_js_keys():
    """Extract all static t('key') calls from static/*.js."""
    keys = set()
    if not STATIC_DIR.exists():
        return keys
    for js_file in STATIC_DIR.glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        keys |= extract_js_keys_from_text(text)
    return keys


def _extract_dynamic_prefixes():
    """Literal prefixes of interpolated t() template calls in static/*.js."""
    prefixes = set()
    if not STATIC_DIR.exists():
        return prefixes
    for js_file in STATIC_DIR.glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        prefixes |= extract_dynamic_prefixes_from_text(text)
    return prefixes


def _load_locale(path):
    """Load a locale JSON file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR loading {path.name}: {e}", file=sys.stderr)
        return None


def main():
    errors = []
    warnings = []

    # Load en.json as canonical
    en_path = LOCALES_DIR / "en.json"
    if not en_path.exists():
        print("FAIL: locales/en.json not found", file=sys.stderr)
        return 1
    en_data = _load_locale(en_path)
    if en_data is None:
        print("FAIL: locales/en.json is invalid JSON", file=sys.stderr)
        return 1
    en_keys = _flatten_keys(en_data)

    if not en_keys:
        print("FAIL: locales/en.json has no translatable keys", file=sys.stderr)
        return 1

    # Extract keys from code
    html_keys = _extract_html_keys()
    js_keys = _extract_js_keys()
    code_keys = html_keys | js_keys

    # Check: every key in code must exist in en.json
    missing_from_en = code_keys - en_keys
    if missing_from_en:
        errors.append(f"Keys referenced in code but missing from en.json: {sorted(missing_from_en)}")

    # Check: every key in en.json should ideally be used in code (warning only,
    # never a failure — the sweep ratchets this count downward over time).
    unused_keys = en_keys - code_keys
    if unused_keys:
        namespaces = Counter(key.split(".")[0] for key in unused_keys)
        top = ", ".join(f"{ns}={count}" for ns, count in namespaces.most_common(8))
        warnings.append(
            f"Unused keys (informational, not a failure): {len(unused_keys)}; top namespaces: {top}"
        )

    # Dynamic template keys cannot be resolved statically; report their prefixes
    # so reviewers can spot-check the namespace instead of guessing.
    dynamic_prefixes = _extract_dynamic_prefixes()
    if dynamic_prefixes:
        warnings.append(
            f"Dynamic t() template keys not statically checkable (informational): {', '.join(sorted(dynamic_prefixes))}"
        )

    # Check all shipped locale files
    locale_files = sorted(LOCALES_DIR.glob("*.json"))
    if not locale_files:
        errors.append("No locale files found in locales/")
        locale_files = []

    coverage_report = []
    for lf in locale_files:
        locale_name = lf.stem
        data = _load_locale(lf)
        if data is None:
            errors.append(f"{lf.name}: invalid JSON")
            continue
        locale_keys = _flatten_keys(data)

        # Check: every en.json key must exist in this locale
        missing = en_keys - locale_keys
        # Check: no extra keys not in en.json
        extra = locale_keys - en_keys

        coverage = (len(en_keys - missing) / len(en_keys) * 100) if en_keys else 0
        coverage_report.append(f"  {locale_name}: {coverage:.1f}% ({len(en_keys - missing)}/{len(en_keys)} keys)")

        if missing:
            errors.append(f"{lf.name}: missing {len(missing)} keys: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")
        if extra:
            errors.append(f"{lf.name}: {len(extra)} extra keys not in en.json: {sorted(extra)[:10]}{'...' if len(extra) > 10 else ''}")

    # Print report
    print("i18n key coverage report:")
    print(f"  en.json canonical keys: {len(en_keys)}")
    print(f"  keys in index.html: {len(html_keys)}")
    print(f"  keys in static/*.js: {len(js_keys)}")
    print(f"  locale files: {len(locale_files)}")
    for line in coverage_report:
        print(line)

    for w in warnings:
        print(f"  WARN: {w}")

    if errors:
        print(f"\nFAIL: {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\nPASS: all locale files have 100% key coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
