# ADR 0033: LaunchBox library XML migration import

**Date:** 2026-09-04
**Status:** Accepted

## Context

OpenBox stores a `launchbox_db_id` field on every game and uses a LaunchBox metadata database (`metadata/launchbox.db`), but has no path to import a user's existing LaunchBox library. Users migrating from LaunchBox must re-add games manually. LaunchBox exports platform libraries as XML files (`Data/Platforms/*.xml`) containing `<Game>` elements with title, path, platform, genre, developer, publisher, rating, emulator ID, and media path fields.

## Decision

Add a two-phase LaunchBox XML migration import:

1. **Parser** (`pkg/parity/parity_launchbox_import.py`): stdlib `xml.etree.ElementTree` only. Maps LaunchBox fields to OpenBox fields via a fixed `_FIELD_MAP`. Unknown fields are ignored. Malformed entries (missing ID or title) are skipped with a count, not an abort.

2. **Preview route** (`POST /api/v2/import/launchbox/preview`): dry-run that parses the XML, deduplicates against the existing library by `launchbox_db_id` first then by name, and returns a report: total in XML, skipped malformed, duplicates, would-import count, emulator IDs found, and a 50-game preview sample.

3. **Apply route** (`POST /api/v2/import/launchbox/apply`): parses the XML and merges via the existing `merge_imported_games` infrastructure. Returns added/found counts plus the emulator ID report.

4. **Emulator mappings are reported, not applied**: LaunchBox `EmulatorId` values are internal to the user's LaunchBox setup and have no OpenBox equivalent. The report surfaces them so the user can configure emulators manually after import.

5. **Deduplication**: `launchbox_db_id` first (exact match), then canonical `game_identity` via `merge_imported_games`. This prevents duplicate imports on re-runs.

## Consequences

- Users can migrate from LaunchBox by pointing at their exported XML.
- No emulator configuration is silently changed — the report makes manual resolution explicit.
- v1 route surface is untouched; both routes are additive `/api/v2/`.
- Re-running import is safe: dedup prevents duplicate games.
