#!/usr/bin/env python3
"""Tests for filter preset chip builder (1.7.2)."""

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

from pkg.parity.parity_filter_presets import rules_to_chips, chips_to_rules, CHIP_LABELS  # noqa: E402


def test_rules_to_chips_basic():
    """rules_to_chips() should convert rules to chip descriptors."""
    rules = {"platform": "PC", "genre": "Action", "favorite": True}
    chips = rules_to_chips(rules)
    assert len(chips) == 3
    for chip in chips:
        assert "key" in chip
        assert "label" in chip
        assert "value" in chip
        assert "display" in chip


def test_rules_to_chips_empty():
    """rules_to_chips() should return empty list for empty rules."""
    assert rules_to_chips({}) == []
    assert rules_to_chips(None) == []


def test_rules_to_chips_bool():
    """rules_to_chips() should display booleans as Yes/No."""
    chips = rules_to_chips({"favorite": True, "hidden": False})
    fav = [c for c in chips if c["key"] == "favorite"][0]
    hid = [c for c in chips if c["key"] == "hidden"][0]
    assert fav["display"] == "Yes"
    assert hid["display"] == "No"


def test_rules_to_chips_string():
    """rules_to_chips() should display string values as-is."""
    chips = rules_to_chips({"platform": "Steam"})
    assert chips[0]["display"] == "Steam"
    assert chips[0]["label"] == "Platform"


def test_rules_to_chips_ignores_unknown():
    """rules_to_chips() should ignore keys not in CHIP_LABELS."""
    chips = rules_to_chips({"platform": "PC", "unknown_key": "value"})
    assert len(chips) == 1
    assert chips[0]["key"] == "platform"


def test_chips_to_rules_basic():
    """chips_to_rules() should convert chips back to rules."""
    chips = [
        {"key": "platform", "value": "PC"},
        {"key": "genre", "value": "Action"},
        {"key": "favorite", "value": True},
    ]
    rules = chips_to_rules(chips)
    assert rules["platform"] == "PC"
    assert rules["genre"] == "Action"
    assert rules["favorite"] is True


def test_chips_to_rules_empty():
    """chips_to_rules() should return empty dict for empty chips."""
    assert chips_to_rules([]) == {}
    assert chips_to_rules(None) == {}


def test_chips_to_rules_ignores_unknown():
    """chips_to_rules() should ignore unknown keys."""
    chips = [{"key": "platform", "value": "PC"}, {"key": "unknown", "value": "x"}]
    rules = chips_to_rules(chips)
    assert "platform" in rules
    assert "unknown" not in rules


def test_chips_to_rules_bool_conversion():
    """chips_to_rules() should convert values to bool for boolean keys."""
    chips = [{"key": "favorite", "value": True}, {"key": "hidden", "value": False}]
    rules = chips_to_rules(chips)
    assert rules["favorite"] is True
    assert rules["hidden"] is False


def test_roundtrip():
    """rules → chips → rules should preserve the original rules."""
    original = {"platform": "PC", "genre": "Action", "favorite": True, "installed": "installed"}
    chips = rules_to_chips(original)
    restored = chips_to_rules(chips)
    assert restored["platform"] == "PC"
    assert restored["genre"] == "Action"
    assert restored["favorite"] is True
    assert restored["installed"] == "installed"


def test_chip_labels_exist():
    """CHIP_LABELS should have entries for all expected keys."""
    expected = {"platform", "genre", "view", "esrb", "progress", "favorite", "hidden", "installed", "query"}
    for key in expected:
        assert key in CHIP_LABELS, f"CHIP_LABELS missing key: {key}"


def run_all_tests():
    tests = [
        test_rules_to_chips_basic,
        test_rules_to_chips_empty,
        test_rules_to_chips_bool,
        test_rules_to_chips_string,
        test_rules_to_chips_ignores_unknown,
        test_chips_to_rules_basic,
        test_chips_to_rules_empty,
        test_chips_to_rules_ignores_unknown,
        test_chips_to_rules_bool_conversion,
        test_roundtrip,
        test_chip_labels_exist,
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
