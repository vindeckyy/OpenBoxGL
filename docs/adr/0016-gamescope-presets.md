# ADR 0016: Gamescope Presets

**Date:** 2026-09-01
**Status:** Accepted

## Context

Steam Deck and handheld users run games through `gamescope` to control resolution, scaling, and window behavior. Prior to 1.7.2, OpenBox only supported gamescope guest detection (`--game-mode`) without offering preset profiles. Users had to manually configure gamescope flags per game or through external tools.

The Deck community commonly uses a small set of display profiles (Steam Deck native, HD, 1080p, 1440p, 4K, integer scale, stretch, borderless) that map to specific gamescope command-line arguments.

## Decision

Add a **gamescope preset system** in `pkg/parity/parity_gamescope.py` that:

1. Defines 8 named presets as structured data (width, height, scaling mode, fullscreen mode).
2. Exposes `list_gamescope_presets()` returning JSON-serializable preset descriptors.
3. Exposes `merge_gamescope_preset(name, extra_args=None)` returning the gamescope command-line arguments for the selected preset, merged with any extra args.
4. Integrates with the Settings → Controller tab for preset selection.
5. Preserves existing `--game-mode` guest detection and nested-gamescope prevention.

Presets are stored as Python data, not user state, keeping the feature dependency-free and deterministic.

## Consequences

- Users can switch display profiles from the UI without editing launch commands.
- ~~The preset list is fixed at 8; custom presets are a future consideration.~~ **Resolved in 1.8.0:** custom presets ship as `settings.gamescope_custom_presets` (≤16, validated in `clean_settings`), resolved by `get/merge/list_gamescope_presets(..., custom_presets=)` with custom names shadowing stock ones, applied at launch (per-game `gamescope_preset` game field wins over the global setting), and editable in Settings → Controller.
- Existing gamescope guest behavior is unchanged.
- `list_gamescope_presets()` returns lists (not tuples) to ensure JSON serialization parity between Python and HTTP responses.
