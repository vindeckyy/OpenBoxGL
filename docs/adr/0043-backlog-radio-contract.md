# ADR 0043: Backlog Radio playlist + abandonment radar contract

**Date:** 2026-09-12
**Status:** Accepted

## Context

The 1.11 plan's T4 lane calls for a "Backlog Radio" — a regenerating playlist
of five games the player will actually finish this week — plus an
"abandonment radar" that surfaces started-then-stalled games that remain
statistically winnable while gently suggesting parking the rest. The plan
describes the persisted artifact as a "managed filter preset", but the filter
preset vocabulary (rules evaluated against library fields) cannot express an
explicit, scored pick list of game IDs; a rule either matches a game or it
does not, and there is no rule for "these exact five".

## Decision

- `pkg/parity/parity_radio.py` is pure analytics over `games` + `history`:
  a 90-day windowed habit model with a 30-day half-life decay (weighted
  seconds per genre/platform, median session length, per-genre
  completion-vs-abandonment, daypart histogram, recently-loved set).
- Candidate score = similarity x habit-fit x freshness, each factor floored
  so a weak factor dampens rather than vetoes. A diversity guard promotes
  novel (genre, platform) signatures only while they stay within
  `DIVERSITY_SCORE_FRACTION` of the top score.
- The playlist materializes into `state["playlists"]` as a managed manual
  entry (`managed == "radio"`): name, `generated_at`, explicit `members`,
  and per-pick `{game_id, score, reasons}`. Manual membership is the honest
  realization of the plan's managed-preset contract. Regeneration reuses the
  previous members as exclusions so two consecutive playlists never repeat
  identical picks.
- Every pick ships reason chips as `{key, params}` i18n references
  (`radio.reason.*`) and every claim must be true of the data; the test
  suite asserts param truth. Thin history (< 8 sessions) falls back to
  rating/freshness picks with `radio.notice.not_enough_history` — the UI
  shows the "Not enough history yet" badge rather than implying habits that
  were never observed.
- The radar partitions started-then-stalled games (>= 14 days since last
  play, >= 15 min observed) into `winnable` (>= 25% invested or estimate
  within the demonstrated completion budget) and `park` (deep stall: >= 30
  days, < 15% invested, estimate > 1.5x budget). `POST .../radar/park` marks
  progress `Paused` and records the park under `state["radio"]["parked"]`.
- Routes: `GET /api/v2/insights/radio`, `POST /api/v2/insights/radio/refresh`,
  `GET /api/v2/insights/radar`, `POST /api/v2/insights/radar/park`. All are
  v2-only; the frozen v1 surface is untouched.
- The Insights panel renders both cards; radio/radar fetch failures degrade
  to "section omitted" so the summary still renders.

## Consequences

- The managed playlist appears in the sidebar/Playlists like any manual
  collection and the picker can scope to it; it self-heals (regenerates)
  when missing, stale (>= 7 days), or hollowed.
- Parked games stay out of the radar until un-parked; parking is a state
  write via `transact_state`, so public-state caches invalidate normally.
- Reason strings are localizable templates, not free text — new reason
  kinds require a locale key in all five shipped locales.
