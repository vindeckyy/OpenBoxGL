# ADR 0005: State cache hierarchy and supervision

Date: 2026-08-20
Status: Accepted
Updated: 2026-09-04 — landed pieces: `pkg/parity/launch_tokens.py` exists (launch-token centralization done, no longer "future"); `pkg/state/imports.py` and `pkg/state/commands.py` exist (deferred moves done); `pkg/state/registry.py` exists (process-registry extraction done). `webapp_state.py` is now a 288-line thin re-export shim. "573-line hybrid" refs below are historical.

## Context

State supervision is split across `pkg/state/cache.py` (7 dicts, 5 locks, file probe TTL 120s/20k, media set 100k), `pkg/state/launch.py` (RUNNING/PROCESSES/SESSION_EVENTS), `pkg/state/media_probe.py`, `pkg/state/sse.py`, and `webapp_state.py` (573-line hybrid shim). Cache invalidation is manual (`bump_media_epoch` clears 6 caches), and `load_state_readonly` returns a mutable reference. Launch placeholder replacement is duplicated in four modules. `webapp_state.py` mixes facade and implementation, handlers import from it instead of `pkg/state`.

## Decision

- Canonical owner is `pkg/state/*`; `webapp_state.py` becomes a thin re-export shim retaining `TOKEN`, `ROOT`, `JOB_MANAGER`, `PROCESS_LOCK`, `RUNNING`, `SSE_*` for one release via `from pkg.state.X import`.

- ~~Introduce `pkg/state/imports.py` (future) for `consolidate_existing_games`, `import_folder_path`, `merge_imported_games`, `auto_import_worker`, `sync_cloud` and `pkg/state/commands.py` for launch commands; defer move to a dedicated PR to avoid import-graph churn.~~ Done: both modules exist; identity/merge helpers live in `pkg/state/imports.py` (no separate `game_identity.py`, see ADR 0008 update).

- ~~Centralize launch token table in `pkg/parity/launch_tokens.py` (future) deduplicating `openbox.py:102`, `webapp_state.py:119`, `pkg/state/launch.py:302`, `parity_emulator_defs.py:112`.~~ Done: `pkg/parity/launch_tokens.py` exists.

- Harden `load_state_readonly` toward `MappingProxyType` or copy-on-write detection; add `CacheEpoch` dataclass to group epochs and single `_invalidate_all()` replacing manual 6-lock sequence.

- Defer high-risk extraction of process registry `RUNNING/PROCESSES/SESSION_EVENTS/PROCESS_LOCK` into `pkg/state/registry.py` with typed `Session` dataclass until a dedicated stability PR with migration test for `active_sessions` JSON.

## Consequences

- No behavior change this release; only `pkg/parity/__init__.py` `_ParityFlatFinder` lands (ADR 0003 companion) proving flat import bridge.

- `runtime_modules.txt` drifts are now caught by `scripts/check_runtime_modules.py` (Stage 2.3 in `check_tests.py`).

- Follow-up PRs will move imports/commands and harden caches with new `tests/test_state_cache.py` covering proxy rejection and epoch invalidation.
