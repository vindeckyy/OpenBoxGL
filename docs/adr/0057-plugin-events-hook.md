# ADR 0057: One `events` hook for plugin lifecycle events

**Date:** 2026-09-22
**Status:** Accepted

## Context

Plugins 2.0 (F1e) adds lifecycle events: `app_startup`, `app_shutdown`,
`scan_finished`, `playtime_milestone`, `game_added`, `game_removed`,
`game_updated`. The naive design is one manifest hook per event
(`app_startup`, `app_shutdown`, …), which grows the manifest surface,
the validator, the runner, and the docs with every new event.

## Decision

One manifest hook named `events`. The stdin payload always carries an
`event` field naming the event plus a small bounded event-specific
payload, e.g. `{"event": "game_added", "game_id": ..., "name": ...}`.
A plugin opts into lifecycle events by declaring `events` in its `hooks`
list and dispatches on `payload["event"]` itself.

## Consequences

- New events are additive data, not API surface: no manifest validator
  changes, no new hook entry points, no v1 contract change (plugin API
  v1 stays frozen; `events` is a 2.0 addition documented in
  `docs/plugin-api.md`).
- Emission sites stay centralized: state-commit diffs in `openbox.py`
  (game_added/removed/updated), import/scan in `pkg/state/imports.py`,
  session completion in `pkg/state/launch.py`, and app start/stop in
  `web_app.py`. All emission is best-effort and bounded by the standard
  per-plugin timeout.
- Event payloads are kept small and stable by convention; each event's
  fields are documented alongside the hook in `docs/plugin-api.md`.
