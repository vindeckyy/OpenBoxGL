# ADR 0029: Wrapped annual report + replay timeline

**Date:** 2026-09-03
**Status:** Accepted

## Context

Players want a year-in-review report from local data and a better way to browse past sessions. M4 adds two additive GET routes and minimal UI.

## Decision

1. **Additive routes, no new state**:
   - `GET /api/v2/insights/wrapped?year=YYYY` returns an annual summary.
   - `GET /api/v2/history/timeline?days=90` returns history grouped by date.
   Both consume existing `games` and `history`; no new persistence or telemetry.
2. **Privacy by construction**: `wrapped_summary` and `timeline_groups` expose only names, cover booleans, aggregates, and recording basenames. No settings, paths (except basename), or credentials leave the API.
3. **Frontend integration**:
   - `static/wrapped.js` is a printable, full-screen dialog launched from the Insights panel header.
   - `static/timeline.js` adds a "Timeline" tab to the existing History dialog.
   - Both reuse `api`, `t`, `media`, and `app:show-game` events.
4. **Print styling**: a dedicated `@media print` block hides the dialog head and sets a light background. No canvas is used in the report, so printing is safe.

## Consequences

- RetroAchievements and save paths are not exposed; `recording` values are basenames only.
- The wrapped dialog is for the current year by default, but the year is user-selectable.
