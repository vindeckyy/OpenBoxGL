# ADR 0028: "What should I play?" Picker

**Date:** 2026-09-03
**Status:** Accepted

## Context

The user wanted a surprise-me style feature with a "wow" factor: a smart picker that asks "what should I play?" and recommends games. This requires a scoring algorithm, a new API route, a dialog UI, and i18n.

## Decision

1. **Dedicated route and parity module**: `POST /api/v2/library/pick` is handled by `handlers/picker.py` and uses `pkg/parity/parity_picker.py`. The handler resolves scope (all, platform, or playlist) and calls the pure function.
2. **Additive scoring with hard filters**: the picker filters by path existence, hidden, players, mood, familiarity, and typical session length. It then scores by play history, favorites, rating, recency, mood keyword match, and session fit, and does weighted random selection among the top 12. This is marked `ponytail:`; upgrades include hand-curated genre taxonomy and a `how_long_to_beat` source.
3. **Client-only rendering**: the result is rendered in `static/picker.js` using the same `media()` and `launch()` helpers as the rest of the UI. No raw styling is added; existing cover and button classes are reused, plus a small `picker-*` rule block for layout.
4. **i18n-first reason strings**: reason templates come from `locales/*.json` with interpolation, so every locale can customize the "why" text.
5. **No settings needed**: the picker is always available via the surprise button, with controls inside the dialog.

## Consequences

- The old random "surprise me" behavior is replaced by the picker dialog (it still has a "Just surprise me" fallback).
- New runtime modules require coverage >= 85% and are listed in `runtime_modules.txt`.
- The same `mood` enum is also used by M1's theming; the picker does not hardcode moods in the UI (populated via i18n).
