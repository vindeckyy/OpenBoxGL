#!/usr/bin/env python3
"""Tests for B4 Big Box + Game Night polish.

Covers: resume-round recovery, wheel keyboard path + focus trap +
reduced-motion static result, snap preload budget + teardown, missing-video
BGM-duck release with cover fallback, per-game MangoHud tri-state with
custom-preset invariants intact, "Excluded N" transparency, video-snaps
toggle default.
Standalone style: `python3 -B tests/test_bigbox_snaps.py`.
"""

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
    SNAP_PRELOAD_BUDGET,
    clean_mangohud_mode,
    resolve_mangohud_enabled,
    restore_bgm_volume,
    select_snap_preloads,
    should_play_snaps,
)
from pkg.parity.parity_party import (  # noqa: E402
    build_party_report,
    should_offer_resume,
)

PARTY_JS = ROOT / "static" / "party.js"
BIGBOX_JS = ROOT / "static" / "bigbox.js"


def sample_games():
    return [
        {"id": 1, "game_id": "g-1", "name": "Mario Kart", "platform": "SNES",
         "path_exists": True, "max_players": 4, "rating": 5},
        {"id": 2, "game_id": "g-2", "name": "Street Fighter", "platform": "Arcade",
         "path_exists": True, "max_players": 2, "rating": 4},
        {"id": 3, "game_id": "g-3", "name": "Solo Quest", "platform": "SNES",
         "path_exists": True, "max_players": 1, "rating": 5},
        {"id": 4, "game_id": "g-4", "name": "Hidden Gem", "platform": "SNES",
         "path_exists": True, "max_players": 4, "rating": 5, "hidden": True},
    ]


def test_resume_offer_mid_queue():
    """Round index > 0 with a non-empty queue offers resume after restart."""
    assert should_offer_resume(["g-1", "g-2", "g-3"], 1) is True
    assert should_offer_resume(["g-1", "g-2"], 1) is True


def test_resume_no_offer_fresh_or_empty():
    """Fresh (index 0), empty, or out-of-range queues offer no resume."""
    assert should_offer_resume(["g-1", "g-2"], 0) is False
    assert should_offer_resume([], 0) is False
    assert should_offer_resume([], 3) is False
    assert should_offer_resume(["g-1"], 5) is False
    assert should_offer_resume(None, 1) is False


def test_report_excluded_count():
    """Report keeps the queue and counts ineligible games as excluded."""
    report = build_party_report(sample_games(), players=2)
    assert set(report["queue"]) == {"g-1", "g-2"}
    assert report["total"] == 4
    assert report["excluded"] == 2, report


def test_report_empty_queue_excludes_all():
    """An empty queue is diagnosable: every input game is excluded."""
    games = [dict(g, max_players=1) for g in sample_games()[:2]]
    report = build_party_report(games, players=2)
    assert report["queue"] == []
    assert report["excluded"] == len(games)


def test_wheel_keyboard_only_path():
    """Wheel is fully operable by keyboard: arrows adjust, Enter acts, N advances, Escape closes."""
    js = PARTY_JS.read_text(encoding="utf-8")
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", "Escape"):
        assert key in js, f"party.js missing keyboard key {key}"
    assert "'n'" in js or '"n"' in js or "toLowerCase() === 'n'" in js, "party.js missing N-for-next-round"


def test_wheel_focus_trap():
    """Overlay traps focus while open and returns focus on close."""
    js = PARTY_JS.read_text(encoding="utf-8")
    assert "Tab" in js, "party.js missing Tab focus-trap handling"
    assert ".focus()" in js, "party.js never moves focus"


def test_wheel_reduced_motion_static_result():
    """Under prefers-reduced-motion the wheel resolves statically (no spin animation)."""
    js = PARTY_JS.read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js or "reducedMotion" in js, "party.js missing reduced-motion check"


def test_snap_preload_budget():
    """Preload budget is visible+1 with teardown on layout switch."""
    assert SNAP_PRELOAD_BUDGET == 2
    order = ["a", "b", "c", "d"]
    assert select_snap_preloads("b", order) == ["b", "c"]
    assert select_snap_preloads("d", order) == ["d"]
    assert select_snap_preloads("zzz", order) == []
    js = BIGBOX_JS.read_text(encoding="utf-8")
    assert "SNAP_PRELOAD" in js or "preload" in js.lower(), "bigbox.js missing preload budget"
    assert "clearVideoSnap" in js, "bigbox.js missing teardown"


def test_missing_video_releases_duck():
    """A missing video restores full BGM volume (never stuck ducked) with cover fallback."""
    assert restore_bgm_volume({}) == 0.6
    assert restore_bgm_volume({"video_bgm_mix": True}) == 0.35
    assert restore_bgm_volume({}) > 0.1, "restore volume must exceed ducked 0.1"
    js = BIGBOX_JS.read_text(encoding="utf-8")
    assert "error" in js, "bigbox.js missing video error handler for 404 snaps"


def test_mangohud_tristate():
    """Per-game inherit/on/off wins over the global toggle."""
    assert resolve_mangohud_enabled({"mangohud": "on"}, {"mangohud_enabled": False}) is True
    assert resolve_mangohud_enabled({"mangohud": "off"}, {"mangohud_enabled": True}) is False
    assert resolve_mangohud_enabled({"mangohud": "inherit"}, {"mangohud_enabled": True}) is True
    assert resolve_mangohud_enabled({"mangohud": "inherit"}, {"mangohud_enabled": False}) is False
    assert resolve_mangohud_enabled({}, {"mangohud_enabled": True}) is True
    assert resolve_mangohud_enabled({}, {}) is False
    assert resolve_mangohud_enabled({"mangohud": "bogus"}, {"mangohud_enabled": True}) is True
    assert clean_mangohud_mode("ON") == "on"
    assert clean_mangohud_mode("off") == "off"
    assert clean_mangohud_mode("anything-else") == "inherit"


def test_custom_presets_intact():
    """Tri-state work keeps <=16 unique bounded-int custom presets resolving."""
    from pkg.parity.parity_gamescope import get_gamescope_preset, list_gamescope_presets
    customs = [{"name": f"c{i}", "width": 1280, "height": 720} for i in range(16)]
    items = list_gamescope_presets(customs)
    names = [name for name, _ in items]
    assert len(names) == len(set(names)), "preset names must stay unique"
    preset = get_gamescope_preset("c0", custom_presets=customs)
    assert preset is not None and preset["args"][:4] == ["-W", "1280", "-H", "720"]
    assert get_gamescope_preset("deck") is not None, "stock presets must stay intact"


def test_video_snaps_default_on_reduced_motion_off():
    """Video snaps default on, explicit off sticks, reduced-motion forces off."""
    assert should_play_snaps({}) is True
    assert should_play_snaps({}, reduced_motion=True) is False
    assert should_play_snaps({"bigbox_video_snaps": False}) is False
    assert should_play_snaps({"bigbox_video_snaps": True}, reduced_motion=True) is False
    assert should_play_snaps({"bigbox_video_snaps": True}) is True


def test_video_snaps_whitelisted():
    """The snaps toggle survives settings sanitization."""
    from settings_schema import KNOWN_SETTINGS
    assert "bigbox_video_snaps" in KNOWN_SETTINGS


def test_party_js_excluded_line():
    """The client renders the Excluded-N transparency line."""
    js = PARTY_JS.read_text(encoding="utf-8")
    assert "party.excluded" in js, "party.js must render t('party.excluded')"


def test_party_js_resume_offer():
    """The client offers resume when the server flags a mid-queue round."""
    js = PARTY_JS.read_text(encoding="utf-8")
    assert "party.resume" in js, "party.js must render t('party.resume')"


TESTS = [
    test_resume_offer_mid_queue,
    test_resume_no_offer_fresh_or_empty,
    test_report_excluded_count,
    test_report_empty_queue_excludes_all,
    test_wheel_keyboard_only_path,
    test_wheel_focus_trap,
    test_wheel_reduced_motion_static_result,
    test_snap_preload_budget,
    test_missing_video_releases_duck,
    test_mangohud_tristate,
    test_custom_presets_intact,
    test_video_snaps_default_on_reduced_motion_off,
    test_video_snaps_whitelisted,
    test_party_js_excluded_line,
    test_party_js_resume_offer,
]


def run_all_tests():
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as error:
            print(f"FAIL {test.__name__}: {error}")
            failures += 1
        except Exception as error:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {type(error).__name__}: {error}")
            failures += 1
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print(f"\nALL PASS ({len(TESTS)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
