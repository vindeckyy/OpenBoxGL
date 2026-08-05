# e03s02 API and failure-boundary sweep evidence

Date: 2026-08-04
Data root: isolated temporary `OPENBOX_DATA_DIR` per probe; real user data untouched.

## Existing adversarial groups (frozen baseline)

Commands: `python3 -B test_bug_sweep_api.py && python3 -B test_parity_api.py`

- Exit: 0 for both.
- Auth, validation (non-object JSON, oversized body), exception mapping, settings, lifecycle, and parity groups all pass with the server alive after each probe.

## I18 — concurrent partial settings saves

Probe: 8 threads POST distinct real settings keys concurrently to a real loopback server, 40 rounds, then GET settings.

- Mechanism: `save_settings` snapshotted settings under `STATE_LOCK`, released the lock, validated for a long time, then committed through `transact_state`. The snapshot base was outside the final write lock.
- Observed with real keys: the per-key merge guard (I2b) already preserved distinct-key updates in all 40 rounds (0 lost updates). The residual was the stale validation/normalization base and the theoretical default-fill window.
- Disposition: fixed in e03s04 — the whole snapshot+validation+commit now runs under one `STATE_LOCK` acquisition via `update_state_with_result` (no `transact_state` re-entry). Regression `test_concurrent_partial_settings_saves` added.

## I17 — native vs web library.json overwrite

Probe A (old pattern): one process loads a snapshot, a second adds a game via `update()`, the first then `save()`s its stale full state.

- Observed: the web game is lost (`final game ids: []`). This is the clobber the issue describes, and it is exactly the `load_state`-then-`save_state` full-overwrite pattern.
- Probe B (current production pattern): both sides commit via `update(mutator)` under the cross-process flock.
- Observed: both changes survive (`game-web` present and `native_touched` true).
- Root cause history: the flock + transactional `update()` routing landed in `8599cf7`/`7b51ed2`; `save_state()` has no production call sites.
- Disposition: verified mitigated. Regression `test_concurrent_update_writers_keep_both_changes` added (two store instances, concurrent update commits, both survive). The full-overwrite `save()` pattern remains a documented footgun with no production caller.

## I15 — optional .env read abort

Inspection: `env_config.load_dotenv` called `path.read_text()` unguarded; an unreadable or non-UTF-8 `.env` would raise through `bootstrap_env` at startup.

- Disposition: fixed in e03s04 (skip unreadable/undecodable files); regression `test_load_dotenv_skips_unreadable_and_binary_files` added.

## I16 — plugin update backup ordering

Inspection: `install_plugin` copies the staged package to `.<id>.installing` before moving the installed copy to `.backups`, and restores the backup if the swap fails. Ordering fix landed in `7b51ed2`; an existing copytree-failure regression already covers the pre-swap path.

- Disposition: verified mitigated; the previously untested mid-swap rollback path (after destination moved to `.backups`) now has a regression in `test_plugins.py`.

## Broad-except scan follow-up

The `except Exception` sites from e03s01 (`parity_gameyfin.py:174,292`, `archives.py:105,185`, `parity_deeplinks.py:126`, `plugins.py:109`, `web_app.py:1834`) all return error results (400 JSON or worker error state) rather than swallowing into a corrupt commit. No defect found at those sites.

## Liveness

- [x] `/api/health` answered 200 after every probe group.
- [x] No probe left a server or temp data root behind.
