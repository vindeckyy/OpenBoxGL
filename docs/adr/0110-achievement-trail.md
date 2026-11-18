# ADR 0110: Achievement Trail — offline RetroAchievements hunt analytics

Status: Accepted (2026-11-17)

## Context

RetroAchievements progress is fetched and cached per game (`retroachievements.py`, `cache/retroachievements/`) and badges render, but nothing computes unlock velocity, completion forecasts, or "what's cheapest next". The 2.2 plan (`docs/NEXT_UPDATE_PLAN-2.2.md` §8) turns that cache into a planning surface. Reads must stay strictly offline. A code audit found a gap the plan did not anticipate: `game_progress` keeps only `earned`/`hardcore` booleans and drops `DateEarned`/`DateEarnedHardcore`, so existing caches contain no unlock dates.

## Decision

- **Additive projection extension (task W2-00).** `game_progress` gains `date_earned`/`date_earned_hardcore` per achievement, and the cache write site (`handlers/settings.py`) persists them. Strictly additive: 2.1 caches still load. Old date-less caches degrade honestly — the UI labels "no date data — refresh to populate" — until an explicit refresh repopulates them.
- `pkg/parity/parity_trail.py` computes pure metrics over normalized achievement lists: velocity in ISO-week buckets (trailing 12 weeks, report the last 8; the hardcore date wins per achievement); ETA `remaining / max(weekly_rate, ε)` with confidence `high` ≥8 active weeks, `medium` 4–7, `low` <4 or zero rate (ETA shown as "—" with a reason); next-up unearned achievements cheapest-first (points ascending, tie by id, cap 10, missing points last); consecutive-week streaks; and the next 100-point milestone.
- Derived cache `cache/trail.json` is keyed by the state signature (`StateStore.signature()`, the same one the SQLite read model uses) plus the max mtime of `cache/retroachievements/`. Stale-while-rebuild per ADR 0101: signature match serves; mismatch recomputes inline when the work is small, else marks `stale:true` and queues a debounced rebuild. Compute is bounded by games-with-cache, not library size.
- Reads never fetch. `POST /api/v2/ra/trail/refresh` is an explicit, bounded, cancellable job through the existing `cached()` wrapper and respects RA rate limits. `GET /api/v2/ra/trail` (paginated; sort `closest|active|longest|points`) and `GET /api/v2/ra/trail/game?game_id=` serve cache only.
- Without credentials (`load_credentials()` falsy) every route returns `available:false` and writes no cache. Problem codes: `RA_DISABLED`, `RA_NOT_LINKED`, `TRAIL_GAME_UNKNOWN`.
- Frontend: a Trail shelf as an Insights section (the `renderRadio` precedent) plus a per-game Trail card in the achievement tabs — CSS bar chart (no chart library), forecast with confidence label, next-up checklist, and an explicit refresh button that polls `GET /api/v2/jobs`.

## Consequences

- Trail works fully offline and degrades to a clean enable-RA empty state; it never hammers the API on a read path.
- Until a user refreshes, date-dependent metrics (velocity, ETA, streaks) are unavailable on old caches and say so rather than guessing.
- A corrupt `trail.json` self-heals by rebuild; it is a projection, never data loss.

## Verification

Planned: `tests/test_trail.py` (year-boundary bucketing, zero/low-rate ETA, confidence thresholds, ordering, streak edges, signature invalidation, date-less old-cache degradation), `tests/test_trail_http.py`, `tests/test_trail_cache.py` (stale-while-rebuild, fast path, corrupt self-heal), and a projection test proving the date fields are additive. Acceptance: plan §6-W2.