# e01s01 baseline evidence

Date: 2026-07-26
Branch: `bug-sweep-2026-07-26`
Commit: `576082569a047c89e0b9b70ddfc68a74598d7110`
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
- `TRADEMARKS.md` (untracked)

Sweep-owned paths begin under `specs/` and `scripts/lib/`. Any later production edit must preserve unrelated hunks in the paths above.

## Full suite

Command: `./run_all_tests.sh`

- Exit: 0
- Duration: 4.04 seconds
- Test modules completed: 26
- unittest-reported test cases: 64 (several repository scripts use module-level assertions, so this is not a complete assertion count)
- Failure/warning signals: none

Modules: test_arcade.py, test_archives.py, test_auto_import.py, test_catalog.py, test_changelog_features.py, test_cloud_sync.py, test_demo_purge.py, test_emulators.py, test_env_config.py, test_importers.py, test_metadata.py, test_packaging.py, test_parity_api.py, test_parity_features.py, test_parity_gameyfin.py, test_parity_integrations.py, test_parity_playnite.py, test_parity_premium.py, test_parity_storefront.py, test_plugins.py, test_retroachievements.py, test_saves.py, test_secrets.py, test_sessions.py, test_stock_themes.py, test_updates.py.

## Compile check

Command: `python3 -m compileall -q .`

- Exit: 0
- Duration: 0.14 seconds
- Signals: none

## Independent critical groups

Command: `python3 -B test_parity_api.py && python3 -B test_sessions.py && python3 -B test_updates.py && python3 -B test_secrets.py && python3 -B test_auto_import.py && python3 -B test_importers.py && python3 -B test_parity_playnite.py && python3 -B test_saves.py && python3 -B test_cloud_sync.py && python3 -B test_plugins.py && python3 -B test_packaging.py`

- Exit: 0
- Duration: 2.37 seconds
- unittest-reported test cases: 34
- Failure/warning signals: none

## Baseline conclusion

The automated baseline is green. This does not close adversarial API or browser discovery; it establishes the comparison point for e01s02–e01s05.
