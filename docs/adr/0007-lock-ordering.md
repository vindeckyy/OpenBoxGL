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
