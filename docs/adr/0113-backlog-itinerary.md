# ADR 0113: Backlog Itinerary — dated play planning

Status: Accepted (2026-11-17)

## Context

Backlog Radio (ADR 0043) answers "what should I play next" one game at a time; nothing produces a dated multi-week plan for the unfinished library. The 2.2 plan (`docs/NEXT_UPDATE_PLAN-2.2.md` §11) adds a plan that uses the best available length estimate, says which estimate it used, and survives real play without constant re-planning.

## Decision

- The plan lives in `state["itinerary"]` (object or `null`, additive, schema stays 7): a single active plan, no history — abandoned plans are overwritten, documented. Warn-and-repair drops a malformed plan, never refusing the library.
- Eligibility reuses `_eligible`/`_is_completed` (`pkg/parity/parity_radio.py`) and the configurable `progress_values` setting — never hardcoded labels — plus explicit exclusion of `"Abandoned"` games, `state["trash"]`, and out-of-scope platforms.
- Estimate chain, first match wins, `est_source` labeled always: stored `game["time_to_beat_hours"]` / IGDB (read the stored field first; fetch via `parity_igdb` only when absent, inside a bounded cancellable job, concurrency 2, cached in `cache/igdb/`) → own-history median of the user's completed same-platform games (≥3 samples, else widen to all platforms) → `default` 8.0 h flagged low-confidence. When RA cache is present, `ra_progress` refines: `est × (1 − earned/total)`.
- Strategies: `quick-wins` (est ascending), `comeback` (playtime descending among unfinished), and `raided` (RA% descending; falls back to `quick-wins` on an empty trail cache). The `franchise` grouping strategy is deferred to 2.3 so 2.2 ships only strategies that work without LaunchBox `series` metadata coverage.
- Capacity math is exact: `capacity_hours = horizon_days × minutes_per_day / 60`; entries sorted by strategy fill greedily until the next would exceed capacity; week buckets; overflow games go to `later[]` and are reported honestly. Wizard inputs are request payload clamped to 7–365 days and 5–960 minutes/day — **not persisted settings** (no `itinerary_*` settings keys).
- Live status is derived on GET, never stored: `done` (progress in the completed set or playtime ≥ est×1.15), `on_track`/`behind` against elapsed capacity since `created_at`, plus plan progress and projected finish week. Re-plan re-runs the strategy on the remainder and pins done entries; stale game ids are dropped with a report.
- Routes: `POST /api/v2/itinerary/plan` (job when an IGDB fetch is needed), `GET /api/v2/itinerary`, `POST /api/v2/itinerary/replan`, `POST /api/v2/itinerary/abandon`, `GET /api/v2/itinerary/estimate?game_id=`. Codes: `ITINERARY_NO_GAMES`, `ITINERARY_INVALID_STRATEGY`.

## Consequences

- The plan is advisory: status derives from real play, not enforcement, and played games flip to done without a re-plan.
- It works with zero credentials — own-history median and the labeled default keep the plan honest; `est_source` chips never present a guess as measured.
- Single active plan means no history surface in 2.2; plan history and `.ics` export are possible 2.3 additions.

## Verification

Planned: `tests/test_itinerary.py` (eligibility matrix, estimate chain + refinement math, each strategy ordering, exact-fit/one-over capacity, week bucketing, replan pinning, derived statuses, empty/all-manual libraries, malformed plan rejection), `tests/test_itinerary_http.py`, `tests/test_itinerary_igdb.py` (creds-absent fallback, per-game isolation, TTL, `est_source` honesty). Acceptance: plan §6-W5, including a zero-credential plan and ≥80% non-default estimates on the fixture via own-history.