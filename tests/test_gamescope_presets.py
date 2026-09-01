#!/usr/bin/env python3
"""Tests for gamescope presets and MangoHud support (1.7.2)."""

import os
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
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_gamescope import (  # noqa: E402
    GAMESCOPE_PRESETS,
    get_gamescope_preset,
    list_gamescope_presets,
    merge_gamescope_preset,
    apply_mangohud_env,
    is_mangohud_available,
)


def test_presets_exist():
    """GAMESCOPE_PRESETS must have at least 5 presets."""
    assert len(GAMESCOPE_PRESETS) >= 5, f"Too few presets: {len(GAMESCOPE_PRESETS)}"
    for name, preset in GAMESCOPE_PRESETS.items():
        assert "label" in preset, f"Preset {name} missing label"
        assert "args" in preset, f"Preset {name} missing args"
        assert isinstance(preset["args"], list), f"Preset {name} args not a list"


def test_get_preset_valid():
    """get_gamescope_preset() should return the preset for a valid name."""
    preset = get_gamescope_preset("deck")
    assert preset is not None, "deck preset not found"
    assert "label" in preset
    assert "args" in preset
    assert "-W" in preset["args"], "deck preset should have -W flag"


def test_get_preset_invalid():
    """get_gamescope_preset() should return None for invalid names."""
    assert get_gamescope_preset("nonexistent") is None
    assert get_gamescope_preset("") is None
    assert get_gamescope_preset(None) is None


def test_get_preset_case_insensitive():
    """get_gamescope_preset() should be case-insensitive."""
    preset = get_gamescope_preset("DECK")
    assert preset is not None, "Case-insensitive lookup failed"
    preset = get_gamescope_preset("Deck")
    assert preset is not None


def test_list_presets():
    """list_gamescope_presets() should return [name, label] pairs."""
    presets = list_gamescope_presets()
    assert len(presets) == len(GAMESCOPE_PRESETS)
    for pair in presets:
        assert isinstance(pair, list) or isinstance(pair, tuple)
        assert len(pair) == 2
        name, label = pair
        assert isinstance(name, str)
        assert isinstance(label, str)
        assert name in GAMESCOPE_PRESETS


def test_merge_preset():
    """merge_gamescope_preset() should return preset args plus extras."""
    args = merge_gamescope_preset("deck", ["--immediate-flips"])
    assert "-W" in args, "Preset args missing"
    assert "--immediate-flips" in args, "Extra args missing"


def test_merge_preset_invalid():
    """merge_gamescope_preset() should return empty list for invalid preset."""
    assert merge_gamescope_preset("nonexistent") == []
    assert merge_gamescope_preset("") == []


def test_merge_preset_no_extras():
    """merge_gamescope_preset() should work without extra args."""
    args = merge_gamescope_preset("1080p")
    assert len(args) > 0
    assert "-W" in args


def test_apply_mangohud_enabled():
    """apply_mangohud_env() should set MANGOHUD=1 when enabled."""
    env = apply_mangohud_env({"PATH": "/usr/bin"}, enabled=True)
    assert env["MANGOHUD"] == "1"
    assert "MANGOHUD_CONFIG" in env
    assert env["PATH"] == "/usr/bin"  # original preserved


def test_apply_mangohud_disabled():
    """apply_mangohud_env() should remove MANGOHUD keys when disabled."""
    env = apply_mangohud_env({"PATH": "/usr/bin", "MANGOHUD": "1", "MANGOHUD_CONFIG": "x"}, enabled=False)
    assert "MANGOHUD" not in env
    assert "MANGOHUD_CONFIG" not in env
    assert env["PATH"] == "/usr/bin"


def test_apply_mangohud_default_env():
    """apply_mangohud_env() should use os.environ when env is None."""
    env = apply_mangohud_env(None, enabled=True)
    assert env["MANGOHUD"] == "1"
    # Should not modify os.environ
    assert "MANGOHUD" not in os.environ or os.environ.get("MANGOHUD") != "1"


def test_apply_mangohud_no_modify_original():
    """apply_mangohud_env() should not modify the input dict."""
    original = {"PATH": "/usr/bin"}
    env = apply_mangohud_env(original, enabled=True)
    assert "MANGOHUD" not in original, "Original dict was modified"
    assert env["MANGOHUD"] == "1"


def test_is_mangohud_available():
    """is_mangohud_available() should return a bool."""
    result = is_mangohud_available()
    assert isinstance(result, bool)


def test_is_mangohud_available_with_fake_which():
    """is_mangohud_available() should use the provided which function."""
    assert is_mangohud_available(which=lambda name: "/usr/bin/mangohud") is True
    assert is_mangohud_available(which=lambda name: None) is False


def test_deck_preset_has_fsr():
    """Steam Deck preset should use FSR upscaling."""
    preset = get_gamescope_preset("deck")
    assert "-F" in preset["args"]
    fsr_idx = preset["args"].index("-F")
    assert preset["args"][fsr_idx + 1] == "fsr"


def test_all_presets_have_unique_labels():
    """All preset labels should be unique."""
    labels = [preset["label"] for preset in GAMESCOPE_PRESETS.values()]
    assert len(labels) == len(set(labels)), "Duplicate preset labels"


def run_all_tests():
    tests = [
        test_presets_exist,
        test_get_preset_valid,
        test_get_preset_invalid,
        test_get_preset_case_insensitive,
        test_list_presets,
        test_merge_preset,
        test_merge_preset_invalid,
        test_merge_preset_no_extras,
        test_apply_mangohud_enabled,
        test_apply_mangohud_disabled,
        test_apply_mangohud_default_env,
        test_apply_mangohud_no_modify_original,
        test_is_mangohud_available,
        test_is_mangohud_available_with_fake_which,
        test_deck_preset_has_fsr,
        test_all_presets_have_unique_labels,
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
