# ADR 0044: Library Time Machine journal contract

**Date:** 2026-09-12
**Status:** Accepted

## Context

OpenBox needs a browsable answer to "what did my library look like then?" and
a safe answer to "undo that edit". The existing library-sync event machinery
already validates catalog events and binds them to state transactions, but it
must also be useful when sync is disabled. The product must be explicit about
what history can and cannot recover: catalog metadata and history are
journaled; media binaries are not.

## Decision

- `library_journal_enabled` defaults to on and records validated local events
  without enabling remote sync. Turning the journal off does no event work.
- `pkg/parity/parity_time_machine.py` exposes three journal-only operations:
  paginated newest-first events with field diffs, deterministic as-of
  materialization, and field-level revert planning/application.
- The v2 routes are:
  `GET /api/v2/library/time-machine/events`,
  `GET /api/v2/library/time-machine/as-of`, and
  `POST /api/v2/library/time-machine/revert`.
- Revert uses preview → apply discipline. A preview carries the current
  `base_token`; apply rejects a changed library with `TM_REVERT_STALE` and
  writes a new event rather than rewriting history.
- Retention and the event cap are honest. Compaction drops records beyond the
  retained window or cap and exposes `horizon`, `gap`, and corruption details
  in read responses; the materializer never invents missing state.
- The surface is v2-only and leaves the frozen v1 routes unchanged.

## Consequences

The timeline and read-only as-of view can work on a local library with no
transport folder or peer. Reverts remain auditable because they appear as
ordinary new journal events. Users cannot use Time Machine as a promise to
restore deleted artwork or other media files; those assets remain outside the
journal contract.
