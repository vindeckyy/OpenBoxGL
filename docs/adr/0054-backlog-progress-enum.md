# ADR 0054: Backlog progress enum reuse and the "Unplayed" display label

**Date:** 2026-09-22
**Status:** Accepted

## Context

Backlog management needs an "unplayed" state for games the user has never
touched, plus a smart-query grammar (`progress:unplayed`, "show unplayed
games"), picker surfacing of backlog titles, and an optional one-time "mark
as Playing?" prompt on first launch. The naive approach adds a new progress
value, but the existing enum (`""`, `Playing`, `Paused`, `Beaten`,
`Completed`, `Mastered`, `Abandoned`) already covers every lifecycle state:
unset (`""`) *is* unplayed, and `Abandoned` covers what backlog planners call
"dropped". Adding a stored `Unplayed` value (or a separate
`completion_status` field) would fork every query, badge, filter preset, and
cloud-sync path that reads `progress`.

## Decision

- The stored vocabulary is unchanged. `""` remains the canonical storage
  for "no status yet". `Unplayed` is a UI/API display alias only:
  - `catalog.canon_progress()` maps `Unplayed` (any case) to `""` on write.
  - `catalog.display_progress()` renders `""` as `Unplayed`.
  - `handlers/library.py` v2 routes accept `Unplayed` but always store `""`;
    v1 is frozen and untouched.
- `catalog.PROGRESS` accepts `Unplayed` so validation stays one call; the
  `cloud_sync.py` mirror is kept synchronized by convention (and
  `tests/test_cloud_sync.py` asserts the mirror equality).
- "Dropped" (typed queries, plan language) maps to `Abandoned` in the query
  grammar; nothing new is stored.
- Query grammar gains `progress:unplayed` (exact: matches `""`),
  `parity_query._progress_matches()` treats stored `""` and the alias
  equivalently, and rule-level matching (`parity_filter_presets`) maps rule
  `Unplayed` to `""`.
- Picker rewards unplayed progress (+5 score) so backlog titles surface; the
  personal `user_rating` field weights 4.0/star vs 3.0 for metadata ratings.
- One-time launch suggestion: `pkg/state/launch.py` marks
  `progress_suggested = True` at session start when the setting
  `backlog_progress_suggest` (default on) is enabled, the game has no
  progress, and `progress_on_first_play` automation did not already set one.
  The frontend asks once; it never writes progress on its own. The setting is
  the kill switch.

## Consequences

- Old libraries need no migration: every unset game is already unplayed.
- Every consumer that reads `game["progress"]` keeps working; consumers that
  display it should use the display alias for unset values.
- F9 (Play Insights) can build on the same `progress` field without a
  parallel vocabulary.
