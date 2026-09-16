# ADR 0049: Consistent snapshot reads for library state

Date: 2026-09-16
Status: Accepted

## Context

Transactions mutate the in-memory cached state in place while holding the
store thread lock (`state_store.py` `update_with_result`, `isolate=False` for
web writes). Several read paths copied that live object *outside* the lock:
`load_state_view()` called `load_state_readonly()` and deep-copied the
returned mapping afterwards, and three handlers (constellation, picker,
library search) read the zero-copy mapping directly. A concurrent request
could therefore observe a half-applied transaction, or fail with
`dictionary changed size during iteration` surfaced as a misleading 400.

## Decision

Reads that feed responses take a consistent snapshot: `JsonStateStore.read_snapshot()`
deep-copies the cached state under the store thread lock and returns the
signature the copy was taken at. `load_state_view()` uses it for the shared
read path, and the three direct zero-copy consumers were moved onto it.

Zero-copy reads (`load_state_readonly`) remain only for projections that hold
the module `STATE_LOCK` for the whole read, such as `_build_public_state`.
Copy-on-write transactions (mutate a detached copy, swap atomically) are the
better long-term model and are covered by the 1.13.0 performance work; this
ADR fixes correctness first without changing the write path.

## Consequences

Readers can briefly serialize behind an in-flight write, and the shared read
path copies the library once per cache miss. Per-request latency at 20k games
stays within the release budget while the copy-on-write/immutable-snapshot
work lands. A regression test (`tests/test_state_snapshot.py`) proves a
reader never observes a partially applied transaction.
