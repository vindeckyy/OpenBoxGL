#!/usr/bin/env python3
"""Tests for per-game launch_env overrides (1.12.0)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers.library import _clean_game_fields, _clean_launch_env  # noqa: E402
from pkg.state.launch import _apply_launch_env  # noqa: E402


def test_clean_from_dict():
    assert _clean_launch_env({"PROTON_NO_ESYNC": "1"}) == {"PROTON_NO_ESYNC": "1"}


def test_clean_from_lines():
    text = "PROTON_NO_ESYNC=1\n# comment\n\nDXVK_HUD=fps"
    assert _clean_launch_env(text) == {"PROTON_NO_ESYNC": "1", "DXVK_HUD": "fps"}
    assert _clean_launch_env("") == {}
    assert _clean_launch_env(None) == {}


def test_clean_rejects_bad_keys():
    for bad in ("1BAD=1", "HAS-DASH=1", "HAS SPACE=1"):
        try:
            _clean_launch_env(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    try:
        _clean_launch_env({"BAD KEY": "x"})
    except ValueError:
        pass
    else:
        raise AssertionError("accepted bad dict key")
    try:
        _clean_launch_env({f"K{i}": "v" for i in range(51)})
    except ValueError:
        pass
    else:
        raise AssertionError("cap not enforced")


def test_apply_merges_over_base():
    base = {"PATH": "/bin", "MANGOHUD": "0"}
    game = {"launch_env": {"MANGOHUD": "1", "DXVK_HUD": "fps"}}
    env = _apply_launch_env(base, game)
    assert env == {"PATH": "/bin", "MANGOHUD": "1", "DXVK_HUD": "fps"}
    assert base["MANGOHUD"] == "0"  # base not mutated


def test_apply_skips_malformed():
    base = {"PATH": "/bin"}
    for game in ({}, {"launch_env": None}, {"launch_env": "oops"},
                 {"launch_env": {"": "x", "A=B": "y", None: "z"}}):
        env = _apply_launch_env(base, game)
        assert env.get("PATH") == "/bin"
        assert "" not in env and "A=B" not in env


def test_launch_confirm_cleaned():
    assert _clean_game_fields({"launch_confirm": 1})["launch_confirm"] is True
    assert _clean_game_fields({})["launch_confirm"] is False


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        fn()
        print(f"ok {name}")
    print("all tests passed")
