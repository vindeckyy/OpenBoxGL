#!/usr/bin/env python3
"""Tests for the OpenBox i18n system (1.7.2).

Verifies:
  - All locale JSON files parse correctly.
  - All locale files have the same key structure as en.json.
  - The check_i18n.py gate script passes.
  - The i18n.js module exists and exports the expected functions.
  - Locale files are served via registered routes.
  - available_locales is exposed in public_settings.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "runtime_modules.txt").is_file():
        return candidate
    if (candidate.parent / "runtime_modules.txt").is_file():
        return candidate.parent
    return candidate

ROOT = _repo_root()
LOCALES_DIR = ROOT / "locales"
SUPPORTED_LOCALES = ["en", "es", "de", "fr", "pt"]


def _flatten_keys(obj, prefix=""):
    keys = set()
    if not isinstance(obj, dict):
        return keys
    for k, v in obj.items():
        full = f"{prefix}.{k}" if prefix else k
        if k == "meta":
            continue
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


def test_locale_files_exist():
    """All supported locale files must exist."""
    for locale in SUPPORTED_LOCALES:
        path = LOCALES_DIR / f"{locale}.json"
        assert path.is_file(), f"Missing locale file: {path}"


def test_locale_files_parse():
    """All locale files must be valid JSON."""
    for locale in SUPPORTED_LOCALES:
        path = LOCALES_DIR / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{locale}.json is not a dict"
        assert "meta" in data, f"{locale}.json missing meta section"
        assert "name" in data["meta"], f"{locale}.json meta missing name"
        assert "native" in data["meta"], f"{locale}.json meta missing native"


def test_key_coverage():
    """All locale files must have the same keys as en.json."""
    en_path = LOCALES_DIR / "en.json"
    en_data = json.loads(en_path.read_text(encoding="utf-8"))
    en_keys = _flatten_keys(en_data)
    assert len(en_keys) > 100, f"en.json has too few keys: {len(en_keys)}"
    for locale in SUPPORTED_LOCALES:
        if locale == "en":
            continue
        path = LOCALES_DIR / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        locale_keys = _flatten_keys(data)
        missing = en_keys - locale_keys
        assert not missing, f"{locale}.json missing {len(missing)} keys: {sorted(missing)[:5]}"
        extra = locale_keys - en_keys
        assert not extra, f"{locale}.json has {len(extra)} extra keys: {sorted(extra)[:5]}"


def test_check_i18n_passes():
    """The check_i18n.py gate script must pass."""
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "check_i18n.py")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"check_i18n.py failed:\n{result.stderr}\n{result.stdout}"


def test_i18n_js_exists():
    """The i18n.js module must exist and export key functions."""
    i18n_path = ROOT / "static" / "i18n.js"
    assert i18n_path.is_file(), "static/i18n.js not found"
    content = i18n_path.read_text(encoding="utf-8")
    for func in ["export", "t(", "init", "setLocale", "getLocale", "getSupportedLocales", "applyTranslations"]:
        assert func in content, f"i18n.js missing: {func}"


def test_locale_routes_registered():
    """Locale file routes must be in PUBLIC_GET_PATHS and GET_TABLE."""
    routes_path = ROOT / "routes.py"
    content = routes_path.read_text(encoding="utf-8")
    for locale in SUPPORTED_LOCALES:
        path = f"/locales/{locale}.json"
        assert path in content, f"Route {path} not in routes.py"


def test_available_locales_in_settings():
    """public_settings must expose available_locales."""
    cache_path = ROOT / "pkg" / "state" / "cache.py"
    content = cache_path.read_text(encoding="utf-8")
    assert "AVAILABLE_LOCALES" in content, "AVAILABLE_LOCALES not in cache.py"
    assert "available_locales" in content, "available_locales not in public_settings output"


def test_locale_selector_in_html():
    """index.html must have a locale selector without the 'planned' note."""
    html_path = ROOT / "index.html"
    content = html_path.read_text(encoding="utf-8")
    assert 'id="localeSetting"' in content, "localeSetting select not in index.html"
    assert "planned for a future release" not in content, "Old 'planned' note still in index.html"


def test_data_i18n_attributes_present():
    """index.html must have data-i18n attributes for translation."""
    html_path = ROOT / "index.html"
    content = html_path.read_text(encoding="utf-8")
    count = len(re.findall(r'data-i18n="', content))
    assert count >= 50, f"Too few data-i18n attributes in index.html: {count}"


def test_i18n_js_in_app_imports():
    """app.js must import i18n.js."""
    app_path = ROOT / "static" / "app.js"
    content = app_path.read_text(encoding="utf-8")
    assert "i18n" in content, "i18n not imported in app.js"


def test_locales_in_build():
    """build_appimage.sh must bundle locale files."""
    build_path = ROOT / "build_appimage.sh"
    content = build_path.read_text(encoding="utf-8")
    assert "locales" in content, "locales not in build_appimage.sh"


def test_locales_in_flatpak():
    """Flatpak manifest must install locale files."""
    flatpak_path = ROOT / "io.openbox.GameLauncher.yml"
    content = flatpak_path.read_text(encoding="utf-8")
    assert "locales" in content, "locales not in Flatpak manifest"


def test_meta_native_names():
    """Each locale meta.native must be the native name of the language."""
    expected = {
        "en": "English",
        "es": "Español",
        "de": "Deutsch",
        "fr": "Français",
        "pt": "Português",
    }
    for locale, native in expected.items():
        path = LOCALES_DIR / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("meta", {}).get("native") == native, \
            f"{locale}.json meta.native is {data.get('meta', {}).get('native')}, expected {native}"


def test_placeholders_preserved():
    """Placeholders like {count}, {n} must be present in all locales."""
    en_data = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
    placeholder_re = re.compile(r"\{(\w+)\}")

    def _collect_placeholders(obj, prefix=""):
        result = set()
        if not isinstance(obj, dict):
            return result
        for k, v in obj.items():
            full = f"{prefix}.{k}" if prefix else k
            if k == "meta":
                continue
            if isinstance(v, dict):
                result |= _collect_placeholders(v, full)
            elif isinstance(v, str):
                result |= {(full, ph) for ph in placeholder_re.findall(v)}
        return result

    en_placeholders = _collect_placeholders(en_data)
    for locale in SUPPORTED_LOCALES:
        if locale == "en":
            continue
        data = json.loads((LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8"))
        locale_placeholders = _collect_placeholders(data)
        missing = en_placeholders - locale_placeholders
        assert not missing, f"{locale}.json missing placeholders: {missing}"


def run_all_tests():
    tests = [
        test_locale_files_exist,
        test_locale_files_parse,
        test_key_coverage,
        test_check_i18n_passes,
        test_i18n_js_exists,
        test_locale_routes_registered,
        test_available_locales_in_settings,
        test_locale_selector_in_html,
        test_data_i18n_attributes_present,
        test_i18n_js_in_app_imports,
        test_locales_in_build,
        test_locales_in_flatpak,
        test_meta_native_names,
        test_placeholders_preserved,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as e:
            print(f"FAIL {test.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {e}")
            failures += 1
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print(f"\nALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
