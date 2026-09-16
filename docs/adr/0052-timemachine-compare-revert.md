# ADR 0052: Time Machine compare and bounded revert

**Date:** 2026-09-16
**Status:** Accepted

## Context

The journal already exposes events, as-of materialization, and field-level
reverts (ADR 0044). Users could not answer "what changed between these two
dates", and the revert surface accepted any catalog field, including identity
and provider-routing fields. The product promise is metadata recovery — never
paths or launch configuration (ADR 0039).

## Decision

- `GET /api/v2/timemachine/compare?a=&b=` materializes both as-of snapshots
  through the existing journal fold and diffs them by `sync_key`:
  `added` (only after), `removed` (only before), and `edited` (present in both
  with changed fields). `summary.re_edited` mirrors `edited`, and `truncated`,
  `corrupt`, `from_as_of`, and `to_as_of` carry the journal's honesty flags
  through. `a` must not be later than `b`.
- Revert apply is bounded by an explicit metadata whitelist in
  `handlers/timemachine.py` (`REVERT_METADATA_WHITELIST`). Preview and apply
  both validate the request fields before the engine runs; a field outside the
  whitelist is rejected with `TM_FIELD_NOT_ALLOWED`. Paths, launch commands,
  install directories, provider ids, `game_id`, and `library_sync_id` are not
  in the whitelist.
- When a revert request omits `fields`, the handler passes the whole whitelist
  instead of the engine's "all catalog fields" default, so a full-row revert
  can never write identity or routing fields.
- `static/timemachine.js` adds a Compare tab rendering the three groups with
  per-field diffs, and the revert confirmation states the metadata-only
  boundary. Compare is read-only; reverts remain timeline actions that append
  a new journal event.

## Consequences

- The compare response is intentionally derived from snapshots rather than raw
  events, so it cannot overstate what the journal retains: pre-horizon state is
  reported truncated, not guessed.
- Reverts stay auditable and bounded; media, paths, and launch setup remain
  outside Time Machine's contract.
