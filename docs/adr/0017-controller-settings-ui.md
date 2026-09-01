# ADR 0017: Controller Settings UI

**Date:** 2026-09-01
**Status:** Accepted

## Context

OpenBox's Big Box mode supports gamepad navigation, but controller configuration was limited to button mapping stored in `appSettings.controller_map`. There was no dedicated settings tab for controller-related options, and no way to test controller connectivity from within the app.

The 1.7.2 release added gamescope presets and MangoHud toggle, both of which are controller/handheld-focused features that need a home in the settings UI.

## Decision

Add a **Settings → Controller tab** that:

1. Houses gamescope preset selection (ADR 0016).
2. Houses the MangoHud performance overlay toggle.
3. Includes a **controller bench** — a live SVG gamepad visualization that reads `navigator.getGamepads()` and renders button/stick state in real time.
4. Uses token-driven CSS colors (`--surface-gamepad-body`, `--surface-gamepad-stick`, `--surface-gamepad-btn`, `--surface-gamepad-btn-active`, `--border-gamepad`, `--text-controller-status`) defined in `static/app.css :root` and overridden in all 5 stock themes.

The SVG gamepad is markup-only (no external assets), keeping the feature dependency-free.

## Consequences

- Controller-related settings are grouped in one tab instead of scattered.
- Users can verify controller connectivity without launching a game.
- 6 new design tokens added to `:root` and all 5 themes; token baseline remains 0.
- `navigator.getGamepads()` polling runs only while the Controller tab is open.
