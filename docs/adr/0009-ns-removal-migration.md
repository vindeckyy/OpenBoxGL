# ADR-0009: Removal of `_ns()` late-binding pattern via dependency injection

Status: accepted
Updated: 2026-09-04 — Phase 1 landed in 1.9.0: `pkg/state/_deps.py` central registry created, `webapp_state._populate_deps()` registers all exported names at import time, the 4 `_ns()` helpers now delegate to `_deps.get()` with `webapp_state` patching as a backward-compat first check.
Date: 2026-08-22

## Context

`pkg/state/` modules (`cache.py`, `launch.py`, `media_probe.py`, `sse.py`)
resolve cross-module names at call time through an identical `_ns()` helper
duplicated in each of the four files:

```python
def _ns(name, default):
    mod = sys.modules.get("webapp_state")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default
```

`_ns()` performs a runtime `getattr` against the `webapp_state` module on every
call.  When `webapp_state` is loaded, the resolved name wins; otherwise the
local default is used.  This pattern exists to break circular imports:
`webapp_state.py` imports from every `pkg/state/*` module, so those modules
cannot import back from `webapp_state` at module scope.

**Scale of the problem**

| Module | `_ns()` call sites | Representative names resolved |
|---|---|---|
| `pkg/state/cache.py` | 24 | `DATA`, `STATE_LOCK`, `load_state`, `load_state_readonly`, `run_plugins`, `_build_public_state`, `_public_state_cached` |
| `pkg/state/launch.py` | 47 | `DATA`, `STATE_LOCK`, `load_state`, `update_state`, `resolve_library_game`, `finish_session`, `sync_cloud`, `run_plugins`, 20+ more |
| `pkg/state/media_probe.py` | 11 | `DATA`, `approved_media_path`, `probe_path`, `download_file`, `bump_media_epoch` |
| `pkg/state/sse.py` | 10 | `load_state`, `transact_state`, `PROCESS_LOCK`, `SESSION_EVENTS`, `broadcast_event`, `emit_notification`, `publish_event` |
| **Total** | **92** | Plus 4 identical function definitions (one per module) |

**Why this is harmful**

1. **Performance** -- every `_ns()` call traverses `sys.modules` and performs
   `hasattr`/`getattr`.  In hot paths (`_build_public_state`,
   `_public_state_cached`, `transact_state`) this adds measurable overhead.
2. **Untraceable dependencies** -- static analysis, IDEs, and `grep` cannot
   resolve `_ns("resolve_library_game", ...)` to its definition.  Module
   contracts are implicit and fragile.
3. **Type safety** -- every `_ns()` result is untyped.  The default value is
   the only type hint, and it is silently discarded when `webapp_state` wins.
4. **Duplication** -- the identical 4-line `_ns()` function is copy-pasted
   across four files.
5. **Test isolation** -- testing a `pkg/state/*` module in isolation requires
   either loading `webapp_state` (pulling in the full import graph) or
   accepting that `_ns()` always returns the default.

## Decision

Replace all 92 `_ns()` call sites with explicit dependency injection.  Each
module that currently calls `_ns()` will receive its dependencies through a
companion `_deps.py` registry module that is populated once at startup and
queried at call time.  This preserves late binding (needed to break the import
cycle) while making the dependency graph explicit, typed, and greppable.

### Architecture

```
pkg/state/_deps.py          -- central dependency registry (typed dict)
pkg/state/cache.py           -- uses deps.get() instead of _ns()
pkg/state/launch.py          -- uses deps.get() instead of _ns()
pkg/state/media_probe.py     -- uses deps.get() instead of _ns()
pkg/state/sse.py             -- uses deps.get() instead of _ns()
webapp_state.py              -- populates deps after all pkg/state/* loaded
```

### The registry (`pkg/state/_deps.py`)

```python
"""Central dependency registry for pkg/state/ modules.

Breaks the circular import between pkg/state/* and webapp_state.py
by providing a typed lookup that is populated once at startup.
"""

from __future__ import annotations
from typing import Any, Callable, Dict

_registry: Dict[str, Any] = {}


def register(name: str, value: Any) -> None:
    """Register a dependency by name.  Called once from webapp_state.py."""
    _registry[name] = value


def get(name: str, default: Any = None) -> Any:
    """Look up a dependency.  Returns *default* if not yet registered."""
    return _registry.get(name, default)
```

### Call-site transformation

Before (current):

```python
load_fn = _ns("load_state", load_state)
state = load_fn()
```

After:

```python
from pkg.state import deps

load_fn = deps.get("load_state", load_state)
state = load_fn()
```

The signature is identical: `deps.get("name", local_default)` replaces
`_ns("name", local_default)`.  The local default remains as the fallback so
modules can be imported and tested without `webapp_state` being loaded.

### Population site (`webapp_state.py`)

After all `pkg.state.*` imports, `webapp_state.py` calls `register()` for each
name that `_ns()` currently resolves:

```python
from pkg.state import deps

deps.register("load_state", load_state)
deps.register("update_state", update_state)
deps.register("DATA", DATA)
deps.register("STATE_LOCK", STATE_LOCK)
# ... one register() per resolved name
```

This is a mechanical extraction of the current import list in `webapp_state.py`.

## Options considered

| Option | Verdict | Reason |
|---|---|---|
| A. Central registry (`_deps.py`) | **Chosen** | Explicit, greppable, typed, single registration site, minimal diff per module |
| B. Constructor injection | Rejected | Would require threading dependency objects through 40+ function signatures and every caller; massive API churn |
| C. Module-level `__getattr__` hook | Rejected | Replaces one implicit mechanism with another; no type safety; still untraceable |
| D. Keep `_ns()` and add type stubs | Rejected | Band-aid; does not solve performance, duplication, or test isolation |
| E. Split `webapp_state.py` then direct import | Deferred | ADR 0008 decomposition is prerequisite; after decomposition, some deps may become direct imports and drop from the registry |

## Phased migration

### Phase 1: Create `_deps.py` and wire population (no behavior change)

1. Create `pkg/state/_deps.py` with `register()` and `get()`.
2. In `webapp_state.py`, add a `_populate_deps()` function that registers every
   name currently resolved by `_ns()`.  Call it at the bottom of
   `webapp_state.py` after all imports.
3. Add `tests/test_deps_registry.py` verifying:
   - `get()` returns `None` (or default) when a name is not registered.
   - `register()` then `get()` round-trips the value.
   - Duplicate `register()` overwrites the previous value.
4. `make check` must pass.

### Phase 2: Migrate `pkg/state/sse.py` (10 sites)

`sse.py` has the fewest call sites and no intra-package `_ns()` dependencies on
other `pkg/state/*` modules, making it the safest starting point.

1. Add `from pkg.state import deps` to `sse.py`.
2. Replace each `_ns("name", default)` with `deps.get("name", default)`.
3. Delete the `_ns()` function from `sse.py`.
4. Add/update tests in `tests/test_sse.py` covering:
   - Functions that resolve `load_state` from deps vs. local default.
   - Functions that resolve `transact_state` from deps vs. local default.
   - Functions that resolve `PROCESS_LOCK` and `SESSION_EVENTS`.
5. `make check` must pass.

### Phase 3: Migrate `pkg/state/media_probe.py` (11 sites)

1. Same mechanical replacement as Phase 2.
2. Delete `_ns()` from `media_probe.py`.
3. Update `tests/test_media_probe.py` covering:
   - `approved_media_path` resolution from deps.
   - `probe_path` resolution from deps.
   - `DATA` resolution from deps.
   - `download_file` and `bump_media_epoch` resolution.
4. `make check` must pass.

### Phase 4: Migrate `pkg/state/cache.py` (24 sites)

1. Replace all `_ns()` calls with `deps.get()`.
2. Delete `_ns()` from `cache.py`.
3. Update `tests/test_state_cache.py` covering:
   - `load_state`, `load_state_readonly`, `update_state_with_result` resolution.
   - `run_plugins` resolution.
   - `DATA`, `STATE_LOCK` resolution.
   - `_build_public_state`, `_public_state_cached` resolution.
   - Cache epoch invalidation with deps active.
4. `make check` must pass.

### Phase 5: Migrate `pkg/state/launch.py` (47 sites)

`launch.py` has the most call sites and the widest dependency surface.

1. Replace all `_ns()` calls with `deps.get()`.
2. Delete `_ns()` from `launch.py`.
3. Update `tests/test_launch.py` (or equivalent) covering:
   - `start_game` 8-phase launch sequence with deps-resolved functions.
   - `finish_session` with deps-resolved `load_state`, `update_state`,
     `run_plugins`, `sync_cloud`.
   - `control_game_session` with deps-resolved `session_event`.
   - `resolve_library_game` with deps-resolved `load_state`.
   - `reconcile_sessions_on_startup` with deps-resolved `_verify_process_identity`.
4. `make check` must pass.

### Phase 6: Remove `webapp_state.py` re-export shim (optional, per ADR 0008)

After all `_ns()` sites are migrated, the reason for `webapp_state.py`'s role as
a late-binding intermediary is removed for the `pkg/state/*` modules.  This phase
is optional and gated on the completion of ADR 0008 decomposition:

1. Remove `_populate_deps()` calls from `webapp_state.py` if all consumers now
   import directly from `pkg.state.*`.
2. Delete `pkg/state/_deps.py` if no late binding is needed.
3. Or keep the registry as the canonical late-binding mechanism if ADR 0008
   decomposition is not yet complete.

## Test strategy

Each phase follows strict TDD:

1. **Before migration**: write a test that calls the function under test with
   `_ns()` active (i.e., with `webapp_state` loaded).  Assert the resolved
   dependency is used.
2. **During migration**: verify that `deps.get()` returns the same value that
   `_ns()` returned.  The test suite must not change behavior.
3. **After migration**: write a test that calls the function with `webapp_state`
   **not** loaded, verifying the local default is used.  This is the test that
   was impossible with `_ns()` (it always loaded `webapp_state`).
4. **Regression**: every phase must pass `make check` (ruff, py_compile, full
   test suite, coverage floors).

### Coverage target

Each migrated module must maintain or improve its current coverage.  The new
`tests/test_deps_registry.py` must achieve 100% coverage on `_deps.py`.

## Consequences

- **Positive**: 92 implicit `sys.modules` lookups replaced with an explicit,
  greppable `deps.get()` call.  Static analysis and IDEs can trace every
  dependency.
- **Positive**: `_ns()` function (4 identical copies) is deleted.  One registry
  module replaces four duplicated helpers.
- **Positive**: Each `pkg/state/*` module can be tested in isolation by not
  populating the registry, exercising the local-default path.
- **Positive**: Performance improves in hot paths where `_ns()` was called per
  invocation (`_build_public_state`, `transact_state`, `start_game`).
- **Negative**: One new file (`_deps.py`) to maintain.  Registration in
  `webapp_state.py` adds ~55 lines of mechanical `register()` calls.
- **Neutral**: No runtime behavior change.  The local-default fallback ensures
  identical semantics whether `webapp_state` is loaded or not.
- **Neutral**: If ADR 0008 decomposition completes, some `deps.get()` calls may
  be replaced with direct imports, shrinking the registry over time.
