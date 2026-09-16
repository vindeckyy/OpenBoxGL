# ADR 0054: Structural-sharing snapshot views for library state

Date: 2026-09-16
Status: Accepted

## Context

ADR 0049 made reads consistent by deep-copying the cached state under the
store lock. That fixed correctness but every read still paid a full
`copy.deepcopy` of the catalog: measured at ~500 ms for a 20k-game library,
plus a second copy inside `load_state_view()` and another after every
`update_state()` mutation. The 1.13 performance budget (p95 `/api/library`
at 20k warm ≤ 250 ms, single write ≤ 50 ms over baseline) is unreachable
while every read and write copies the whole catalog.

## Decision

`load()`, `load_state_view()` and `update()` return **copy-on-write views**
over a detached snapshot instead of deep copies:

- `SnapshotDict`/`SnapshotList` are `dict`/`list` subclasses whose first
  mutation detaches only that node and its ancestors (`_detach`), so writes
  through a view can never reach the snapshot or the live store cache.
  Reads share containers. Iteration and `__getitem__` wrap nested containers
  so `for game in view["games"]: game["x"] = 1` stays isolated. `json.dumps`,
  equality, `len`, slicing, pickling and `isinstance(x, dict)` keep working
  because the views are real dict/list subclasses.
- `JsonStateStore.load()` caches one deep copy per file signature
  (`_snapshot_state`, invalidated by `_remember`/`_clear_cache`/`_write_unlocked`)
  and hands out views over it. `read_snapshot()` (ADR 0049) still returns a
  detached copy for callers that need one.
- `update_with_result()` no longer deep-copies the committed state to build
  its return value; `update()` wraps the committed state in a view.
- `pkg.state.cache.state_view_snapshot()` is the documented helper tests use
  to prove repeated `load_state_view()` calls share one structure; a state
  mutation replaces it.

Journaling (P2-3) builds on the same write path: `state_store.TrackedGame`
records the previous value of every field assignment during a transaction,
and `parity_library_sync.begin_catalog_tracking` / `record_tracked_changes`
project catalogs only for records the mutator touched. A no-op or
settings-only write performs no catalog projection and creates no sync
metadata.

## Consequences

- Warm reads are O(1); the one deep copy per file signature remains (cold
  reads, external file edits, and reads immediately after a write when the
  store snapshot cache was invalidated).
- A view held across a subsequent in-place transaction can observe that
  transaction's changes; views are intended to be consumed synchronously by
  one request, exactly like the previous per-call copy. ADR 0049 consistency
  for response reads is preserved because the snapshot itself is taken under
  the store thread lock and is never mutated in place.
- `update_state()` returns a view, not a detached plain copy. Callers that
  retain it across transactions should call `copy.deepcopy()`; existing
  in-place mutation through the returned value remains isolated.
- New runtime code lives in `state_store.py` (no new module).

## Tests

- `tests/test_perf_20k_paths.py` proves snapshot sharing (identity of
  `state_view_snapshot()` and zero deepcopy calls on warm reads), view
  mutation isolation, snapshot replacement on mutation, zero-copy warm
  writes, and targeted journaling.
- `tests/test_state_snapshot.py` (ADR 0049), `tests/test_state_v4.py`,
  `tests/test_state_readonly.py`, `tests/test_perf_state.py` and
  `tests/test_perf_cache.py` cover the detached-copy contracts.
