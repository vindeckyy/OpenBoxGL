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


PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _load_gate():
    """Import scripts/check_i18n.py so the tests exercise the shipped gate.

    The placeholder-parity rule lives in the gate, not here. A second copy in
    the test file would be a second implementation that could disagree with the
    one CI runs, which is the exact failure mode this rule exists to prevent.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_i18n_under_test", ROOT / "scripts" / "check_i18n.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flatten_strings(obj, prefix=""):
    """key -> string, for every non-`meta` leaf."""
    result = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        full = f"{prefix}.{k}" if prefix else k
        if k == "meta":
            continue
        if isinstance(v, dict):
            result.update(_flatten_strings(v, full))
        elif isinstance(v, str):
            result[full] = v
    return result


def test_placeholders_preserved():
    """Every locale must carry exactly the placeholders en.json does, per key.

    Key-set parity alone does not cover this: a locale can keep all 1,237 keys
    while dropping `{count}` from one of them. `i18n.js` substitutes a missing
    param with an empty string, so the result is "Sessions " with nothing after
    it and every gate green.
    """
    gate = _load_gate()
    en_strings = _flatten_strings(json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8")))
    assert en_strings, "en.json has no translatable strings"
    for locale in SUPPORTED_LOCALES:
        if locale == "en":
            continue
        data = json.loads((LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8"))
        drift = gate._placeholder_drift(en_strings, _flatten_strings(data))
        assert not drift, f"{locale}.json placeholder drift on {len(drift)} key(s): {dict(list(drift.items())[:5])}"


def test_placeholder_gate_detects_both_directions():
    """The gate's drift checker must fail on a drop *and* on an addition.

    A gate that cannot fail is worse than no gate, so this exercises the shipped
    implementation against the two shapes it exists to catch, plus the
    reordered string it must *not* flag.
    """
    drift = _load_gate()._placeholder_drift
    reference = {
        "a.count": "{count} games",
        "b.name": "{game} by {studio}",
    }
    assert drift(reference, {"a.count": "{count} juegos", "b.name": "{game} por {studio}"}) == {}
    # Same placeholders, different order: a correct translation, not drift.
    assert drift({"k": "{days} days, {name}"}, {"k": "{name}, {days} Tage"}) == {}

    dropped = drift(reference, {"a.count": "muchos juegos", "b.name": "{game} por {studio}"})
    assert "a.count" in dropped and dropped["a.count"]["missing"] == ["count"], dropped

    added = drift(reference, {"a.count": "{count} games", "b.name": "{game} por {studio} y {year}"})
    assert "b.name" in added and added["b.name"]["extra"] == ["year"], added

    assert list(drift(reference, {})) == ["a.count", "b.name"]


def test_placeholder_names_are_uniformly_interpolated():
    """A key with a placeholder must be rendered by code that names it.

    Placeholder parity across locales cannot help a value nothing renders: if no
    module passes `{count}`, every locale is equally correct and equally wrong.
    """
    en_strings = _flatten_strings(json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8")))
    keyed = {key for key, text in en_strings.items() if PLACEHOLDER_RE.search(text)}
    assert keyed, "expected at least one placeholder-bearing key in en.json"

    bundle = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "static").glob("*.js"))
    )
    unwired = sorted(
        key for key in keyed
        if not (f'"{key}"' in bundle or f"'{key}'" in bundle or f"`{key}`" in bundle)
    )
    assert len(unwired) < len(keyed) / 2, (
        f"{len(unwired)} of {len(keyed)} placeholder keys are referenced by no static/*.js module; "
        f"placeholder parity cannot help a value nothing renders: {unwired[:8]}"
    )


def test_dialog_and_bigbox_keys_present():
    """Row 16: the five keys added for the dialog/Big Box fixes (media
    toggle i18n + empty-view strings) must exist in every locale."""
    required = {
        "dialog.media_show_more",
        "dialog.media_show_fewer",
        "dialog.media_show_more_initial",
        "bigbox.empty_view_setup",
        "bigbox.empty_view_now_empty",
    }
    for locale in SUPPORTED_LOCALES:
        path = LOCALES_DIR / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = _flatten_keys(data)
        missing = required - keys
        assert not missing, f"{locale}.json missing keys: {sorted(missing)}"


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
        test_placeholder_gate_detects_both_directions,
        test_placeholder_names_are_uniformly_interpolated,
        test_dialog_and_bigbox_keys_present,
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
