# ADR 0026: Mood Match Adaptive Cover Theming

**Date:** 2026-09-03
**Status:** Accepted

## Context

OpenBox 1.9.0 adds a "wow" moment: the selected/focused game's cover art drives a live color palette that tints selected cards, the detail hero, Big Box, and primary actions. This requires new CSS custom properties, JS palette extraction from same-origin cover images, and two persisted settings.

## Decision

1. **Token-only styling**: all adaptive colors are exposed as five `:root` tokens — `--mood-primary`, `--mood-ink`, `--mood-secondary`, `--mood-glow`, `--mood-tint` — defined in `static/app.css` and all five theme files. Components opt in by referencing `var(--mood-*)`; no raw hex is introduced in component rules.
2. **JS-driven overrides**: `static/mood.js` extracts a palette from the current cover and sets the five tokens as inline styles on `document.documentElement`, adding the `.mood-active` class. When disabled or no cover is present, the tokens are cleared, which restores `:root` defaults.
3. **Progressive, decorative-only**: only borders, shadows, hero tints, and hover states may change. `--text`, `--focus`, form colors, and accessibility-critical tokens are never overwritten. Ink color is chosen by WCAG contrast ratio against the primary color.
4. **Settings gates**: `mood_match_enabled` and `mood_match_bigbox` are persisted booleans, defaulted to `false`. The latter is ignored unless the former is `true` (enforced in both `clean_settings` and the JS application logic).
5. **64-bin RGB quantizer** is intentionally lightweight. The palette is extracted from a 48×48 canvas; dominant color is selected from 4×4×4 RGB bins, with luminance and saturation gating. This is marked `ponytail:` with a documented upgrade path to median-cut/k-means if users report poor palettes.
6. **No new routes or runtime dependencies**; only settings whitelisting, `public_settings`, the `static/mood.js` module, and CSS updates are required.

## Consequences

- All stock themes must define the five `--mood-*` tokens so disabled state renders identically to before.
- Cover media stays same-origin and un-CORS-tainted because `media()` resolves to `/api/media` on the same host; `img.crossOrigin = 'anonymous'` is a defensive extra.
- The feature auto-disables on platforms that lack canvas or `Image.decode()`, degrading gracefully.
- Big Box and library views share one palette state; the only behavioral difference is the `mood_match_bigbox` gate.
