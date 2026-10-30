# ADR 0101: SQLite read model rebuilds off the request path

Status: Accepted for 2.0.

## Context

`pkg/state/sqlite_readmodel.py` has been an optional read path since 1.7.2 (ADR 0014),
graduated in 1.12.0 to self-enable above 5,000 games (ADR 0032, ADR 0047). The search
route rebuilt the projection **on the request thread**: `ensure_fresh()` compared the
state signature and, when it had changed, deleted and reinserted every row before the
request could answer. On a large library that is seconds of blocked work on the first
search after any write.

The plan for 2.0 (section 13.3) asks for the opposite shape: a write invalidates the
projection, a debounced background thread rebuilds it, and a read that finds a stale
projection answers from the canonical JSON state.

## Decision

- A read never rebuilds. `is_fresh(signature)` decides whether the projection may
  answer; a stale projection falls back to the JSON path and the response says
  `source: "json"`.
- A write invalidates the projection and queues a debounced rebuild
  (`schedule_rebuild()`), which runs on a background thread. The debounce is 0.75s, and
  a pending rebuild runs anyway once it has waited 5s, so a steady stream of requests
  cannot postpone it for ever.
- The rebuild takes its snapshot from the registered state provider on the background
  thread, so the request that scheduled it does not read the library either.
- The projection is still not the source of truth. The canonical state is the JSON
  file; `sqlite_readmodel.db` lives beside it, is excluded from backups, and deleting
  it costs one rebuild.
- The parity check is the safety net. After every background rebuild the projection is
  compared with the canonical game ids. A mismatch logs a warning and drops the model
  back to the JSON path until a later rebuild proves the rows match again.
- A corrupt database is deleted and rebuilt. It is a projection, never data loss.
- The model stays opt-out with `OPENBOX_ENABLE_SQLITE_READ=0` and keeps its 5,000-game
  self-enable threshold.

## Consequences

- The first search after a write answers from JSON and says so; the next one is
  served from the projection.
- No request blocks on a rebuild, so the read latency no longer depends on the size of
  the library.
- `ensure_fresh()` keeps its synchronous behaviour for callers that ask for an explicit
  rebuild; the request path no longer uses it.

## Verification

`tests/test_sqlite_readmodel.py` (the debounced rebuild, the state provider, the
corrupt database, the parity mismatch) and `tests/test_search_http.py` (a stale
projection answers `source: "json"` and the rebuild lands in the background).
