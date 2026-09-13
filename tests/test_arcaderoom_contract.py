#!/usr/bin/env python3
"""Standalone contract checks for the T5 Arcade Room canvas surface."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT / "static" / "arcaderoom.js"


def source() -> str:
    assert SOURCE_PATH.is_file(), "static/arcaderoom.js is missing"
    return SOURCE_PATH.read_text(encoding="utf-8")


def test_module_is_dependency_free_and_uses_app_contracts():
    text = source()
    imports = re.findall(r"^import\s+.*?from\s+['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    assert imports, "Arcade Room must use ES module imports"
    assert set(imports) == {"./util.js", "./state.js"}, imports
    assert "escapeHtml" in text and "defaultControllerMap" in text
    assert "var(--surface-deep)" in text
    assert "var(--text)" in text
    assert not re.search(r"from\s+['\"](?:https?:|npm:|@)", text)


def test_canvas_has_dpr_resize_and_reduced_motion_paths():
    text = source()
    assert "class ArcadeRoom" in text
    assert "resizeCanvas" in text
    assert "devicePixelRatio" in text
    assert "canvas.width = pixelWidth" in text
    assert "canvas.height = pixelHeight" in text
    assert "setTransform(dpr, 0, 0, dpr, 0, 0)" in text
    assert "prefers-reduced-motion: reduce" in text
    assert "reducedMotion" in text
    assert "MUSEUM · STATIC" in text


def test_texture_cache_is_bounded_and_supports_art_and_video():
    text = source()
    assert "class TextureCache" in text
    assert "MAX_TEXTURE_CACHE = 48" in text
    assert "this.entries = new Map()" in text
    assert "while (this.entries.size > this.maxEntries)" in text
    assert "loadVideo" in text
    assert "this.entries.delete(oldest)" in text
    assert "textureCache" in text


def test_texture_cache_eviction_disposes_video_entries_like_clear():
    text = source()
    assert "_disposeEntry(entry)" in text
    assert "texture.pause?.(); texture.removeAttribute?.('src'); texture.load?.()" in text
    assert "const entry = this.entries.get(oldest)" in text
    assert "this.entries.delete(oldest);\n      this._disposeEntry(entry)" in text
    assert "for (const entry of this.entries.values()) this._disposeEntry(entry);" in text


def test_screensaver_zero_disables_museum_idle_timer():
    text = source()
    assert "const delay = numberOr(configured, DEFAULT_IDLE_MS)" in text
    assert "return delay === 0 ? 0 : clamp(delay, MIN_IDLE_MS, MAX_IDLE_MS)" in text
    assert "this.idleMilliseconds === 0" in text


def test_room_virtualizes_zones_and_cabinets():
    text = source()
    assert "function groupArcadeZones" in text
    assert "function visibleWindow" in text
    assert "CABINET_WINDOW_RADIUS" in text
    assert "ZONE_WINDOW_RADIUS" in text
    assert "visibleWindow(zone.games.length" in text
    assert "for (let offset = -ZONE_WINDOW_RADIUS" in text


def test_empty_and_missing_art_fallbacks_are_real_render_paths():
    text = source()
    assert "drawEmptyRoom" in text
    assert "Your library has no cabinets yet." in text
    assert "drawMissingArt" in text
    assert "MARQUEE" in text
    assert "EMPTY ZONE" in text
    assert "SCREEN" in text
    assert "if (!screenDrawn)" in text


def test_navigation_covers_keyboard_gamepad_and_touch():
    text = source()
    for key in ("keydown", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", "Escape"):
        assert key in text, f"missing keyboard navigation contract: {key}"
    assert "getGamepads" in text
    assert "gamepadState" in text
    assert "const edge = key => current[key] && !this.gamepadState[key]" in text
    for event_name in ("touchstart", "touchend", "pointerdown", "pointerup"):
        assert event_name in text, f"missing touch/pointer navigation contract: {event_name}"
    assert "SWIPE_DISTANCE" in text


def test_museum_scheduler_uses_screensaver_idle_setting_and_real_facts():
    text = source()
    assert "scheduleMuseumMode" in text
    assert "setTimeout(() =>" in text
    assert "screensaver_seconds" in text
    assert "enterMuseumMode" in text and "exitMuseumMode" in text
    assert "buildMuseumFacts" in text
    assert "ra_achievements_earned" in text
    assert "ra_achievements_total" in text
    assert "No exhibit notes yet." in text
    assert "if (year)" in text and "if (developer)" in text and "if (genre)" in text


def test_existing_show_game_and_launch_paths_have_event_hooks():
    text = source()
    assert "app:show-game" in text
    assert "app:launch-game" in text
    assert "dispatchDocumentEvent(SHOW_GAME_EVENT" in text
    assert "dispatchDocumentEvent(LAUNCH_EVENT" in text
    assert "import('./sessions.js')" in text
    assert "app:state-refreshed" in text
    assert "app:open-arcade-room" in text
    assert "window.OpenBoxArcadeRoom" in text


def test_public_controller_exports_are_present():
    text = source()
    export_block = re.search(r"export\s*\{(?P<body>.*?)\};", text, re.DOTALL)
    assert export_block, "Arcade Room must export its controller API"
    body = export_block.group("body")
    for name in (
        "ArcadeRoom", "TextureCache", "createArcadeRoom", "openArcadeRoom", "closeArcadeRoom",
        "renderArcadeRoom", "moveArcadeRoom", "enterMuseumMode", "exitMuseumMode",
        "scheduleMuseumMode", "resizeArcadeCanvas",
    ):
        assert re.search(rf"\b{name}\b", body), f"missing export: {name}"


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
