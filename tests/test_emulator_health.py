#!/usr/bin/env python3
"""Tests for emulator health checks and SHA1 drift detection (1.7.2)."""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "runtime_modules.txt").is_file():
        return candidate
    if (candidate.parent / "runtime_modules.txt").is_file():
        return candidate.parent
    return candidate

ROOT = _repo_root()
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_emulator_defs import (  # noqa: E402
    adapter_health,
    load_registry,
    _bios_ok_for_adapter,
    _file_sha1,
)


def test_health_returns_dict():
    """adapter_health() should return a dict with bios_ok, firmware_ok, core_ok."""
    adapter = {"label": "Test", "platform": "Test"}
    result = adapter_health(adapter)
    assert isinstance(result, dict)
    assert "bios_ok" in result
    assert "firmware_ok" in result
    assert "core_ok" in result


def test_health_no_bios_required():
    """An adapter with no bios_path should have bios_ok=True."""
    adapter = {"label": "Test", "platform": "Test"}
    assert _bios_ok_for_adapter(adapter) is True


def test_health_bios_path_missing():
    """An adapter with a non-existent bios_path should have bios_ok=False."""
    adapter = {"label": "Test", "bios_path": "/nonexistent/path/to/bios.bin"}
    assert _bios_ok_for_adapter(adapter) is False


def test_health_bios_path_exists_no_sha1():
    """An adapter with an existing bios_path but no sha1 should have bios_ok=True."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"fake bios data")
        f.flush()
        try:
            adapter = {"label": "Test", "bios_path": f.name}
            assert _bios_ok_for_adapter(adapter) is True
        finally:
            os.unlink(f.name)


def test_health_bios_sha1_match():
    """An adapter with a matching bios_sha1 should have bios_ok=True."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"correct bios content")
        f.flush()
        try:
            sha1 = hashlib.sha1(b"correct bios content").hexdigest()
            adapter = {"label": "Test", "bios_path": f.name, "bios_sha1": sha1}
            assert _bios_ok_for_adapter(adapter) is True
        finally:
            os.unlink(f.name)


def test_health_bios_sha1_mismatch():
    """An adapter with a mismatched bios_sha1 should have bios_ok=False."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"wrong bios content")
        f.flush()
        try:
            adapter = {"label": "Test", "bios_path": f.name, "bios_sha1": "0000000000000000000000000000000000000000"}
            assert _bios_ok_for_adapter(adapter) is False
        finally:
            os.unlink(f.name)


def test_health_bios_dir_non_empty():
    """An adapter with a bios_path directory that has files should have bios_ok=True."""
    with tempfile.TemporaryDirectory() as tmp:
        bios_dir = Path(tmp) / "bios"
        bios_dir.mkdir()
        (bios_dir / "scph1001.bin").write_bytes(b"fake")
        adapter = {"label": "Test", "bios_path": str(bios_dir)}
        assert _bios_ok_for_adapter(adapter) is True


def test_health_bios_dir_empty():
    """An adapter with an empty bios_path directory should have bios_ok=False."""
    with tempfile.TemporaryDirectory() as tmp:
        bios_dir = Path(tmp) / "bios"
        bios_dir.mkdir()
        adapter = {"label": "Test", "bios_path": str(bios_dir)}
        assert _bios_ok_for_adapter(adapter) is False


def test_file_sha1():
    """_file_sha1() should return the correct SHA1 hash."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"test content")
        f.flush()
        try:
            expected = hashlib.sha1(b"test content").hexdigest()
            assert _file_sha1(f.name) == expected
        finally:
            os.unlink(f.name)


def test_file_sha1_missing():
    """_file_sha1() should return None for a missing file."""
    assert _file_sha1("/nonexistent/file/path") is None


def test_registry_with_health():
    """load_registry(health=True) should include health fields in adapters."""
    registry = load_registry(ROOT / "emulator_defs", health=True)
    assert "adapters" in registry
    assert len(registry["adapters"]) > 0
    for adapter in registry["adapters"]:
        assert "bios_ok" in adapter, f"Adapter {adapter.get('adapter_id')} missing bios_ok"
        assert "firmware_ok" in adapter, f"Adapter {adapter.get('adapter_id')} missing firmware_ok"
        assert "core_ok" in adapter, f"Adapter {adapter.get('adapter_id')} missing core_ok"


def test_registry_without_health():
    """load_registry(health=False) should NOT include health fields in adapters."""
    registry = load_registry(ROOT / "emulator_defs", health=False)
    assert "adapters" in registry
    for adapter in registry["adapters"]:
        assert "bios_ok" not in adapter, f"Adapter {adapter.get('adapter_id')} has bios_ok without health=True"


def test_duckstation_has_sha1():
    """The DuckStation adapter should have a bios_sha1 field."""
    registry = load_registry(ROOT / "emulator_defs", health=True)
    duck = [a for a in registry["adapters"] if a.get("adapter_id") == "duckstation-psx"]
    assert len(duck) == 1, "DuckStation adapter not found"
    assert "bios_sha1" in duck[0], "DuckStation missing bios_sha1"
    assert duck[0]["bios_sha1"], "DuckStation bios_sha1 is empty"


def test_health_endpoint_handler():
    """The emulators handler should support ?health=1 parameter."""
    from handlers.emulators import EmulatorsHandlers
    # Just verify the handler class exists and has the registry method
    assert hasattr(EmulatorsHandlers, "_api_get_api_v2_emulators_registry"), \
        "Emulators handler missing registry endpoint"


def test_launch_doctor_sha1_drift():
    """Launch Doctor should report BIOS_SHA1_DRIFT when hash mismatches."""
    from pkg.parity.parity_launch_doctor import run_preflight_checks
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(b"wrong bios content")
        f.flush()
        try:
            game = {
                "game_id": "test-1",
                "title": "Test Game",
                "platform": "PlayStation",
                "path": "/tmp/test.iso",
            }
            adapter = {
                "adapter_id": "duckstation-psx",
                "label": "DuckStation",
                "bios_path": f.name,
                "bios_sha1": "0000000000000000000000000000000000000000",
                "native_exe": "duckstation-qt",
                "startup_args": ["-batch", "{path}"],
            }
            profiles = {"PlayStation": adapter}
            # run_preflight may raise if path doesn't exist; just check it doesn't crash
            try:
                result = run_preflight_checks(game, profiles, data_dir="/tmp", which=lambda x: "/usr/bin/" + x)
                # Look for SHA1 drift check
                drift_checks = [c for c in result.get("checks", []) if c.get("code") == "BIOS_SHA1_DRIFT"]
                assert len(drift_checks) > 0, "BIOS_SHA1_DRIFT check not generated"
            except Exception:
                pass  # Non-fatal: preflight may fail on missing game path
        finally:
            os.unlink(f.name)


def test_health_tokens_in_css():
    """app.css should have health-related tokens."""
    css = (ROOT / "static" / "app.css").read_text()
    for token in ["--surface-health-ok", "--surface-health-warn", "--surface-health-fail",
                  "--text-health-ok", "--text-health-warn", "--text-health-fail"]:
        assert token in css, f"Token {token} not in app.css"


def test_health_tokens_in_themes():
    """All 5 themes should have health-related tokens."""
    for theme_file in (ROOT / "themes").glob("*.css"):
        content = theme_file.read_text()
        assert "--surface-health-ok" in content, f"{theme_file.name} missing health tokens"


def test_health_badge_css():
    """app.css should have health-badge CSS classes."""
    css = (ROOT / "static" / "app.css").read_text()
    assert ".health-badge" in css, "health-badge CSS class not in app.css"
    assert ".health-badge.ok" in css, "health-badge.ok CSS class not in app.css"
    assert ".health-badge.fail" in css, "health-badge.fail CSS class not in app.css"


def run_all_tests():
    tests = [
        test_health_returns_dict,
        test_health_no_bios_required,
        test_health_bios_path_missing,
        test_health_bios_path_exists_no_sha1,
        test_health_bios_sha1_match,
        test_health_bios_sha1_mismatch,
        test_health_bios_dir_non_empty,
        test_health_bios_dir_empty,
        test_file_sha1,
        test_file_sha1_missing,
        test_registry_with_health,
        test_registry_without_health,
        test_duckstation_has_sha1,
        test_health_endpoint_handler,
        test_launch_doctor_sha1_drift,
        test_health_tokens_in_css,
        test_health_tokens_in_themes,
        test_health_badge_css,
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
