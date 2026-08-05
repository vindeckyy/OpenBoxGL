# e03s04 fix ledger

Date: 2026-08-04
Rule: every accepted fix has a mechanism, a regression, and red-green evidence. "Green at baseline + fix" counts only where the regression is a durable guard for a verified mitigation.

## I15 — .env startup abort (fixed)

- Mechanism: `env_config.load_dotenv` called `path.read_text()` unguarded; unreadable or non-UTF-8 `.env` raised through `bootstrap_env` and aborted startup.
- Trigger: any `.env` with binary bytes or 000 permissions in scope.
- Fix: `load_dotenv` wraps the read in `try/except (OSError, UnicodeDecodeError)` and returns `{}` for unreadable files (smallest shared root: all callers of `load_dotenv`/`bootstrap_env` are protected).
- Regression: `test_env_config.py::test_load_dotenv_skips_unreadable_and_binary_files` (binary file, chmod-000 file, and `bootstrap_env` with a binary file in scope).
- Evidence: `python3 -B test_env_config.py` PASS.

## I18 — settings snapshot outside the final write lock (fixed)

- Mechanism: `save_settings` snapshotted settings under `STATE_LOCK`, released it, validated against that stale base, then committed. The validation/normalization base could go stale relative to a concurrent save.
- Trigger: two overlapping partial settings POSTs in a `ThreadingHTTPServer` process.
- Fix: `save_settings` now holds `STATE_LOCK` across snapshot, validation, and commit, delegating to `_save_settings_locked`; the commit uses `update_state_with_result` directly (calling `transact_state` while holding the non-reentrant lock would deadlock). In-process saves can no longer interleave; cross-process behavior is unchanged (flock + per-key merge guard).
- Regression: `test_bug_sweep_api.py::test_concurrent_partial_settings_saves` (8 threads, 8 rounds, distinct real keys, all must persist).
- Evidence: `python3 -B test_bug_sweep_api.py` PASS; I18 probe 0/40 lost updates after the fix.

## I17 — native vs web library overwrite (verified mitigated)

- Mechanism: the full-state `load_state()`-then-`save_state()` overwrite clobbers concurrent `update()` commits (probe A lost the web game). The production code no longer uses that pattern: all native commits route through `_commit` → `update_state`, and `save_state` has no production callers. The cross-process flock serializes the transactional read-modify-write.
- Fix: none required. Durable regression `test_backend_hardening.py::test_concurrent_update_writers_keep_both_changes` (two `JsonStateStore` instances on one path, concurrent `update()` commits, both changes survive).
- Evidence: `python3 -B test_backend_hardening.py` PASS; probe B preserved both changes.

## I16 — plugin update backup ordering (verified mitigated)

- Mechanism: the old ordering could leave a plugin missing on failed copy. Current `install_plugin` stages the copy first, then moves the installed copy to `.backups`, then swaps, and restores the backup if the swap fails.
- Fix: none required. The mid-swap rollback path was untested; added a regression in `test_plugins.py` that forces the staging→destination `Path.replace` to fail after the destination moved to `.backups` and asserts the previous version is restored and the `.backups` dir is cleaned.
- Evidence: `python3 -B test_plugins.py` PASS.

## I14 — Gameyfin install poll cap (fixed)

- Mechanism: `watchGameyfinInstall(gameyfinId, attempts = 40)` polled every 1.5s and threw at ~60s, aborting the UI wait while the server-side download continued. Install workers always terminate to done/error, so a fixed 60s cap is not a real wedge guard.
- Fix: default attempts 1200 (30 minutes at 1.5s).
- Evidence: `grep -q 'attempts = 1200' index.html`; full suite PASS.

## I12 — favicon 404 (fixed)

- Mechanism: no icon route and no `<link rel="icon">`, so browsers requested `/favicon.ico` and logged a 404 each load.
- Fix: `_do_GET` serves the repo `openbox.svg` at `/favicon.svg` and `/favicon.ico`; index.html head references the SVG.
- Evidence: e03s03 browser journeys show both routes 200 and `failedRequests: []`; two browser runs confirmed before/after.
