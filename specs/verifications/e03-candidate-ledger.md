# e03 candidate ledger

## Candidates

- id: I15
  area: env / startup
  priority: P3
  evidence: `specs/verifications/e03s02-api-sweep.md`; `test_env_config.py::test_load_dotenv_skips_unreadable_and_binary_files`
  reproduction_count: 1 inspection plus deterministic unit regression
  status: fixed
  reason: `load_dotenv` now skips unreadable or non-UTF-8 optional `.env` files instead of aborting startup.
  owner_story: e03s04
  verify: `python3 -B test_env_config.py`

- id: I18
  area: settings / concurrency
  priority: P2
  evidence: `specs/verifications/e03s02-api-sweep.md`; `test_bug_sweep_api.py::test_concurrent_partial_settings_saves`
  reproduction_count: 40-round concurrent HTTP probe (distinct keys preserved; stale-base window closed by fix)
  status: fixed
  reason: settings snapshot, validation, and commit now run under one `STATE_LOCK` acquisition via `update_state_with_result`; the write base can no longer go stale in-process.
  owner_story: e03s04
  verify: `python3 -B test_bug_sweep_api.py`

- id: I17
  area: persistence / cross-process
  priority: P2
  evidence: `specs/verifications/e03s02-api-sweep.md`; `test_backend_hardening.py::test_concurrent_update_writers_keep_both_changes`
  reproduction_count: 2 deterministic store probes (old pattern loses data; current pattern preserves both)
  status: mitigated
  reason: the flock + transactional `update()` path (landed in `8599cf7`) already protects native and web commits; the clobbering full-state `save()` pattern has no production caller. Regression locks the behavior in.
  owner_story: e03s04
  verify: `python3 -B test_backend_hardening.py`

- id: I16
  area: plugins
  priority: P3
  evidence: `specs/verifications/e03s02-api-sweep.md`; `test_plugins.py` mid-swap rollback regression
  reproduction_count: 1 inspection plus deterministic swap-failure regression
  status: mitigated
  reason: copytree-before-backup ordering and the swap rollback landed in `7b51ed2`; the previously untested mid-swap rollback path now has direct coverage.
  owner_story: e03s04
  verify: `python3 -B test_plugins.py`

- id: I14
  area: ui / gameyfin
  priority: P3
  evidence: `index.html` (`watchGameyfinInstall` default attempts 1200); code review
  reproduction_count: 1 inspection (frontend constant)
  status: fixed
  reason: the 40-attempt/60-second poll cap was too aggressive for legitimate long downloads; raised to 1200 attempts (30 minutes). Server workers always terminate to done/error, so the cap is only a wedge safety net.
  owner_story: e03s04
  verify: `grep -q 'attempts = 1200' index.html`

- id: I12
  area: ui / browser
  priority: P3
  evidence: `specs/verifications/e03s03-browser.json`; `specs/verifications/e03s03-browser-sweep.md`
  reproduction_count: 2 browser runs (initial 404), then fixed (200 on both routes)
  status: fixed
  reason: `/favicon.svg` and `/favicon.ico` now serve the repo icon; the index references the SVG; no 404 on initial load.
  owner_story: e03s03/e03s04
  verify: `node scripts/e03s03-browser-sweep.mjs` journeys show `failedRequests: []`

- id: I13
  area: handheld
  priority: P3
  evidence: `specs/verifications/e03s01-baseline.md` (backlog seed)
  reproduction_count: 0 (requires live Deck/Bazzite hardware)
  status: deferred
  reason: optional live-hardware confirm beyond the nested emulation harness; no local regression can exercise real QAM/overlay focus. Outside the fix budget.
  owner_story: e03s05
  recommended_action: exercise on real Deck hardware in a future hardware-gated session.

All e03 candidates now have evidence, priority, reproduction count, owner story, and terminal current disposition.
