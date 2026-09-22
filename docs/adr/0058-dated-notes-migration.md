# ADR 0058: Dated game notes migration from legacy single string

**Date:** 2026-09-22
**Status:** Accepted

## Context

Game notes were a single optional string (`notes` on the game record). Backlog
management needs dated journal entries with add/edit/delete, and the plan
originally proposed a new notes table. A separate table adds a second storage
for one game's data, a second sync surface, and a migration that rewrites
every library on upgrade.

## Decision

- `notes` stays on the game record and becomes a list of
  `{ts, text}` entries, migrated on read, not on write:
  - `catalog.normalize_notes()`: a legacy string becomes one entry with
    `ts: ""`; `None`/missing becomes `[]`; malformed entries are dropped.
  - `pkg/state/cache.py` projects normalized entries, so the frontend and
    every consumer always see the list shape; old libraries load unchanged
    and are rewritten only when the user actually edits notes.
- Writes go through `POST /api/v2/library/notes/add|update|delete` (v2 only;
  v1 is frozen). Entries are capped at 2000 characters; empty-text entries
  are dropped; updates stamp `ts` when the text changes.
- Search stays honest: `parity_query._notes_text()` joins entry texts, so
  `notes:` predicates and the query haystack work for both legacy strings
  and entries without callers knowing the difference.
- Backups: `parity_export.EXPORT_GAME_FIELDS` carries `notes` as before, so
  normalized entries ride the existing export path.

## Consequences

- No bulk migration runs at startup; libraries upgrade lazily and
  reversibly (a legacy string and its migrated entry render the same text).
- Any caller that assumed `notes` is a string must use the normalized form
  (query, haystack, and projection already do).
- Entry timestamps use ISO local time of the edit; the legacy entry has an
  empty timestamp and sorts/looks like the original note.
