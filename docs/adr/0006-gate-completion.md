# ADR 0006: Gate completion and frontend coverage

Date: 2026-08-20
Status: Accepted
Updated: 2026-09-04 — `API_V1` now lives at `static/util.js:120` (not :115); the v1 surface is frozen at 60 contract entries (`v1_contracts.json`). The stale `check_tests.py` "parallel workers" comment flagged below is fixed as of this update.

## Context

`scripts/check_tests.py` ran ruff, v1 contract, version sync, py_compile, coverage, and tokens, but not frontend lint (`scripts/check_frontend.py` was orphaned, zero refs) and not runtime module drift (`runtime_modules.txt` manual). CI duplicated eslint inline (`ci.yml:71`) while `make check` never linted JS. `API_V1` in `static/util.js:115` (now :120) covered 38 routes while 15 legacy paths (`/api/premium/media-packs`, `/api/gameyfin/test`, `/api/themes/open-folder`, `/api/media/cleanup`, `/api/bigbox/mode`, etc.) stayed on fallback via `state.js:54 target=API_V1[..]||path`. The v1 surface is now frozen at 60 contract entries.

## Decision

- Wire `check_frontend.py` as Stage 2.6 in `check_tests.py` and `check_runtime_modules.py` as Stage 2.3. `check_runtime_modules.py` asserts every line exists, no duplicates, and required globs `handlers/*.py`, `pkg/state/*.py`, `pkg/parity/*.py`, `routes/*.py` are listed.

- Fix `check_frontend.py` to prefer `scripts/eslint.config.mjs` then `static/eslint.config.mjs` and not silently degrade when npm is present on CI.

- Extend `routes.py:V1_ALIASED_PREFIXES` with 14 legacy prefixes: `premium/media-packs`, `premium/media-packs/apply`, `storefront/import`, `gameyfin/test`, `import/scummvm`, `import/rpcs3`, `import/vita3k`, `themes/open-folder`, `ra/inject`, `media/cleanup`, `saves/scan/apply`, `bigbox/mode`, `games/bulk-wizard`, `extra/launch`.

- Extend `static/util.js:API_V1` with matching snake_case keys (`premium_media_packs`, `gameyfin_test`, etc.) so `state.js:api` auto-translates via `API_V1[path.replace...]||path` with fallback for one release.

- Fix `check_tests.py` comment claiming parallel workers while loop is serial due to gamescope X collision; keep serial.

## Consequences

- `make check` and CI `gate` now run the same frontend lint; orphan removed. Required drift fails the gate before AppImage ships.

- 14 v1 routes become available as additive aliases; `gen_v1_contracts.py` and `check_v1_contract.py` pass without version bump; legacy paths still work via fallback.

- No handler renamed, no route deleted, no coverage floor lowered.
