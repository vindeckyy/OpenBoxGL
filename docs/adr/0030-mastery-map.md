# ADR 0030: Mastery Map completionist dashboard

**Date:** 2026-09-04
**Status:** Accepted

## Context

Completionist players want a per-platform and per-decade breakdown of local progress states alongside RetroAchievements hardcore progress. The RA cache on disk (`<state_dir>/retroachievements/*.json`, see `retroachievements.py` `cached()` + `match_game()`) already holds everything needed — no new network calls are required or desired (rate-limit safe).

## Decision

1. **Additive route, no new state**: `GET /api/v2/insights/mastery` returns `{platforms, overall, decades}` where each bucket holds `{never, played, beaten, completed, mastered, ra_tracked, ra_mastered, ra_avg_progress, total}`. Progress classification is local (`progress` field, falling back to playtime/play_count); RA fields come only from the disk cache and default to zeros when the cache is missing.
2. **Frontend integration**: `static/mastery.js` opens `#masteryDialog` from Tools → Mastery, rendering stacked horizontal bars per platform (or decade via the decade filter) with tokenized `--mastery-*` segments defined in `app.css :root` and all five stock themes. Clicking a segment dispatches `app:show-game` and sets the library platform filter via existing sidebar state.
3. **i18n**: state labels use a static `stateLabels()` map (literal `t('mastery.*')` keys evaluated at render time) so `check_i18n.py` sees every key and locale switching stays correct.

## Consequences

- RA data is never fetched by this feature; stale cache reads are acceptable for a dashboard and keep the feature offline-safe.
- One additive GET route; route-count test bumped accordingly.
