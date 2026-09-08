# ADR 0036: Manual/shelf entries

**Date:** 2026-09-04
**Status:** Accepted; expanded in 1.10.0

## Context

Before 1.9.0, OpenBox required a local file path for every game entry. Users with
physical media (cartridges, discs), board games, or console games that do not
have a local executable still needed a catalog record without a fake path. The
initial 1.9.0 implementation was deliberately small; 1.10.0 completed the
normal UI workflow without changing the record model.

## Decision

Add a single route for manual entries:

1. **`POST /api/v2/library/manual-entry`**: accepts a game object with only `name` required. Platform, genre, developer, etc. are optional. The entry is marked with `manual_entry: true` and `path: ""` so it can be filtered or displayed differently in the UI.

2. **Reuses existing infrastructure**: `_clean_game_fields`, `_clean_game_lists`, `_apply_game_misc`, and `transact_state` — no new game model or abstraction.

3. **No path validation**: manual entries skip the path existence/symlink/file checks that `save_game` enforces, since there is no executable.

4. **Downstream reuse**: manual entries flow through the same library as all other games — they appear in search, facets, Wrapped, Mastery, and exports. The `manual_entry` flag drives the Shelf filter and launchability projection. The 1.10.0 UI adds create/edit/convert actions; conversion attaches a verified local path without changing the stable game identity.

## Consequences

- Users can track physical/board/console games in their OpenBox library.
- No new abstraction or speculative catalog system.
- The `manual_entry` flag keeps catalog membership separate from launchability and supports the Shelf filter and editor.
- v1 route surface untouched; additive `/api/v2/` route.
