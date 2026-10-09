# ADR-0007: Lock ordering for state, process, and file synchronization

Status: Accepted
Date: 2026-08-22

## Context

OpenBox uses three independent synchronization primitives to protect
concurrent access to shared state:

1. **PROCESS_LOCK** (`pkg/state/launch.py`) — a `threading.Lock` guarding
   the in-memory process registry (`RUNNING`, `PROCESSES`).
2. **STATE_LOCK** (`pkg/state/cache.py`) — a `threading.Lock` guarding
   cache-coherent reads of the persisted JSON state and all projection
   caches built on top of it.
3. **_file_lock** (`state_store.py`) — an `fcntl.flock()` advisory file
   lock serializing reads and writes to `library.json` on disk.

A fourth, finer-grained lock — `_thread_lock` (`threading.RLock` inside
`JsonStateStore`) — protects the in-memory cache within a single store
instance and is always acquired before `_file_lock`.

Multiple threads touch these locks concurrently: HTTP request handlers
through `transact_state`, game launch watchers through `finish_session`,
SSE publishers through `_build_public_state`, and background plugin
refreshers through `_refresh_plugins`. A lock-ordering violation between
any two of the three outer locks would create a deadlock window.

## Decision

Adopt and enforce a strict **sequential non-nesting** policy for the three
outer locks:

```
PROCESS_LOCK  ←→  STATE_LOCK  ←→  _file_lock
```

No thread may hold two of these three locks simultaneously. Each is
acquired, used, and released before any other outer lock is acquired.

Within `state_store.py`, the internal ordering is:

```
_thread_lock  →  _file_lock
```

`_thread_lock` is always acquired first; `_file_lock` is always nested
inside it. This pairing is confined to `JsonStateStore` and never leaks
across module boundaries.

When `STATE_LOCK` is held and a state read or write is required, the
caller invokes `load_state_readonly()` or `update_state()` which acquire
`_thread_lock` → `_file_lock` internally. This is safe because
`PROCESS_LOCK` is never held at the same time.

The lock-acquisition order in the two main concurrency paths is:

| Path | Order |
|---|---|
| HTTP handler → `transact_state` | STATE_LOCK → _thread_lock → _file_lock → (release all) → cache locks |
| Watcher thread → `finish_session` | PROCESS_LOCK → (release) → STATE_LOCK → _thread_lock → _file_lock → (release all) → PROCESS_LOCK |

Cache-level locks (`PLUGIN_LIBRARY_LOCK`, `PUBLIC_STATE_LOCK`,
`PUBLIC_SETTINGS_LOCK`, `STATE_VIEW_LOCK`, etc.) are leaf locks acquired
only after `STATE_LOCK` is released, or in `_refresh_plugins` where they
are nested inside `STATE_LOCK` but never inside `PROCESS_LOCK`.

## Consequences

- **Deadlock prevention**: the three outer locks can never form a cycle
  because no thread holds two of them at once.
- **Performance**: cache-level locks remain fine-grained and do not
  contend with the process registry.
- **Testability**: new code acquiring any of these three locks must
  demonstrate it does not hold another; review checklist updated.
- **Readability**: inline `///< lock ordering:` comments at every
  acquisition site document the constraint at the point of use.

## Addendum (1.16.1): the coalesced-write lock

`state_store.py` adds a fourth lock to the in-process order, `_coalesce_lock`, which
guards the pending coalesced snapshot and its flush timer. The order inside a store is:

```
_thread_lock  ->  _file_lock  ->  _coalesce_lock
```

Two rules follow from it:

- A full write (`_write_unlocked`) clears the pending coalesced snapshot, because the
  in-memory state it was written from already carries that mutation. Flushing the
  pending snapshot after a full write would roll the newer commit back.
- A coalesced mutation publishes its pending snapshot while still holding
  `_thread_lock`, so a full commit cannot land between the mutation and its
  publication.

Trade-off: when another process changed `library.json`, a full write reloads the file
from disk, and a coalesced mutation that had not yet flushed is dropped rather than
written over the other process's change. Before 1.16.1 the opposite happened and that
change was lost. The drop is intended; it is recorded here so a later reader does not
restore the old behaviour.

Covered by `tests/test_perf_writes.py` (`CoalesceCommitOrderingTests`).
