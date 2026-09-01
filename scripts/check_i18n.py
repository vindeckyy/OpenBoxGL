#!/usr/bin/env python3
"""Verify i18n key coverage across all shipped locale files.

Scans index.html for data-i18n attributes and static/*.js for t('key') calls.
Loads every locales/*.json. Fails if:
  - any key referenced in code is missing from en.json
  - any key in en.json is missing from a shipped locale JSON
  - any locale JSON has extra keys not in en.json
Reports coverage percentage per locale.

Run directly: python3 scripts/check_i18n.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "locales"
INDEX_HTML = ROOT / "index.html"
STATIC_DIR = ROOT / "static"

# Attributes that carry i18n keys
I18N_ATTRS = ("data-i18n", "data-i18n-placeholder", "data-i18n-title", "data-i18n-aria-label")

# Regex for t('key') or t("key") calls in JS
T_CALL_RE = re.compile(r"""\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]""")


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
    """Extract all t('key') calls from static/*.js."""
    keys = set()
    if not STATIC_DIR.exists():
        return keys
    for js_file in STATIC_DIR.glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        keys |= set(T_CALL_RE.findall(text))
    return keys


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

    # Check: every key in en.json should ideally be used in code (warning only)
    unused_keys = en_keys - code_keys
    if unused_keys:
        warnings.append(f"Keys in en.json not referenced in code (informational): {len(unused_keys)} keys")

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
