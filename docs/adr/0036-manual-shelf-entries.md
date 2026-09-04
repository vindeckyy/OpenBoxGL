# ADR 0036: Manual/shelf entries

**Date:** 2026-09-04
**Status:** Accepted

## Context

OpenBox requires a local file path for every game entry. Users with physical media (cartridges, discs), board games, or console games that don't have a local executable want to track these in their library without a fake path. This is a stretch feature for 1.9.0 — minimal scope, one route, no new abstraction.

## Decision

Add a single route for manual entries:

1. **`POST /api/v2/library/manual-entry`**: accepts a game object with only `name` required. Platform, genre, developer, etc. are optional. The entry is marked with `manual_entry: true` and `path: ""` so it can be filtered or displayed differently in the UI.

2. **Reuses existing infrastructure**: `_clean_game_fields`, `_clean_game_lists`, `_apply_game_misc`, and `transact_state` — no new game model or abstraction.

3. **No path validation**: manual entries skip the path existence/symlink/file checks that `save_game` enforces, since there is no executable.

4. **Downstream reuse**: manual entries flow through the same library as all other games — they appear in search, facets, Wrapped, Mastery, etc. The `manual_entry` flag is available for UI filtering but no UI changes are required for the stretch scope.

## Consequences

- Users can track physical/board/console games in their OpenBox library.
- No new abstraction or speculative catalog system.
- The `manual_entry` flag enables future UI differentiation without requiring it now.
- v1 route surface untouched; additive `/api/v2/` route.
