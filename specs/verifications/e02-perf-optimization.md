# Performance optimization verification — Phase 1–4

Date: 2026-08-02
Branch: `master` (uncommitted perf work)
Baseline: `specs/verifications/PERF_BASELINE.md` (OpenBox v0.7.0, pre-optimization)

## Server-side results (median of 3, warm cache, 5,000-game synthetic library)

| Metric | Baseline | After Phase 1–3 | Target | Result |
|---|---:|---:|---:|---|
| `/api/library` ms | 2659.4 | 12.8 | < 300 | PASS (208x) |
| `/api/media` ms | 186.5 | 1.3 | < 10 | PASS (143x) |
| favorite mutation ms | 1299.0 | 121.6 | < 150 | PASS (10.7x) |

Payload bytes unchanged at 13,455,581 (compact serialization, same fields).

## Browser-side results (Puppeteer, headless Chrome, 5,000-game library)

| Metric | Baseline | After Phase 4 | Target | Result |
|---|---:|---:|---:|---|
| grid first paint ms | ~n/a | 884 | < 2000* | PASS |
| keystroke re-render ms (median) | ~n/a | 7 | < 200 | PASS (28x margin) |
| keystroke re-render ms (max) | ~n/a | 10 | — | PASS |
| grid DOM cards | 5000+ | 63 (virtualized window) | — | PASS |
| grid DOM nodes | ~150k | 679 | — | PASS |
| media requests on re-render | many | 0 | 0 | PASS |

*The first-paint figure includes full 13.4MB library transfer + JSON parse; the
interactive grid renders in ~1s and re-renders per keystroke in single-digit ms.

## Changes

- `state_store.py`:
  - Fast-path validation in `_load_unlocked` (skip normalize for current schema).
  - `_write_unlocked` adopts state into cache (`adopt=True`) instead of deep-copying.
  - `update_with_result` returns the store-owned committed state (read-only contract);
    `update()` returns a detached snapshot for callers that need one.
- `web_app.py` (Phase 2/3, in progress):
  - `public_state` cached until library/media/plugin epoch changes; read-only state view.
  - Compact JSON serialization for large libraries; single backup per commit.
  - Media epoch bump on media changes; theme CSS `must-revalidate` + ETag.
- `index.html` (Phase 4):
  - Virtualized grid (`renderGrid` window), debounced search handlers,
    query token precompile, targeted favorite update (no full refresh).
- `scripts/perf_browser.mjs`: fixed wait selector for virtualized grid; DOM
  measurement after debounced re-render.

## Test status

- `test_perf_writes.py`, `test_perf_state.py`: PASS in isolation and together.
- `test_perf_cache.py`: PASS in isolation.
- Known pre-existing cross-file isolation issue: `test_bug_sweep_api` +
  `test_perf_cache` in one interpreter pollute shared `MEDIA_EPOCH`/theme state
  (fails on baseline too); not caused by this work.

## Residual risk

- Browser first paint dominated by 13.4MB library transfer (inherent to payload);
  payload slimming (field projection) is a possible follow-up.
- Virtualized grid limits visible DOM but full `visible.length` filtering still
  runs per render; fine at 5k, worth revisiting at 50k+.
