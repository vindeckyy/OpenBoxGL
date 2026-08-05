# e03s01 baseline evidence

Date: 2026-08-04
Branch: master (repository policy: single branch)
Commit: `b5465178e03f17e647b7582c714b2e0c83f7146e`
Python: `3.12.3`

## Working-tree safety snapshot

Pre-existing user-owned paths before planning/execution:

- `DISCLAIMER.md`
- `README.md`
- `SECURITY.md`
- `index.html`
- `openbox.desktop`
- `openbox.metainfo.xml`
- `test_packaging.py`
- `TRADEMARKS.md`

Untracked paths at freeze time: `.commandcode/` (user-owned, preserved).

Sweep-owned paths begin under `specs/` and `scripts/lib/`. Any production edit must preserve unrelated hunks in the user-owned paths above.

## Full suite

Command: `./run_all_tests.sh`

- Exit: 0
- Test modules completed: 35
- Failure/warning signals: none

Modules: test_arcade.py, test_archives.py, test_auto_import.py, test_backend_followup.py, test_backend_hardening.py, test_bug_sweep_api.py, test_catalog.py, test_changelog_features.py, test_cloud_sync.py, test_demo_purge.py, test_emulators.py, test_env_config.py, test_gamescope_deck_emu.py, test_importers.py, test_logging.py, test_metadata.py, test_packaging.py, test_parity_api.py, test_parity_features.py, test_parity_gamescope.py, test_parity_gameyfin.py, test_parity_integrations.py, test_parity_playnite.py, test_parity_premium.py, test_parity_storefront.py, test_perf_cache.py, test_perf_state.py, test_perf_writes.py, test_plugins.py, test_retroachievements.py, test_saves.py, test_secrets.py, test_sessions.py, test_stock_themes.py, test_updates.py.

## Compile check

Command: `python3 -m compileall -q .`

- Exit: 0
- Signals: none

## Static scan notes (candidate seeds, not defects)

- Broad `except Exception` at `parity_gameyfin.py:174,292`, `archives.py:105,185`, `parity_deeplinks.py:126`, `plugins.py:109`, `web_app.py:1834`. Reviewed during e03s02; each returns an error result rather than swallowing into a corrupt commit (details in e03s02).
- Subprocess call sites use `start_new_session=True`; HTTP-facing subprocess boundaries catch `subprocess.SubprocessError` (lines 1499, 1822, 2445, 2458, 2849, 2868).
- File I/O uses `with` blocks or `os.fdopen`; no unclosed-handle pattern found in `web_app.py`/`openbox.py`/`state_store.py`.
- Open issue seeds to probe in e03s02/e03s03: I18 (settings snapshot outside final write lock), I17 (native vs web library.json overwrite), I16 (plugin backup ordering), I15 (`.env` read abort), I14 (Gameyfin poll cap).

## Baseline conclusion

The automated baseline is green at the frozen SHA. This establishes the comparison point for e03s02–e03s05; it does not close adversarial API or browser discovery.
