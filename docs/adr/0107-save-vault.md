# ADR 0107: Save Vault — indexed save snapshots

Status: Accepted (2026-11-17)

## Context

2.1 discovers save paths and `backup_saves` makes ad-hoc archives (`saves.py`), but there is no indexed, retained, browsable snapshot history. The 2.2 plan (`docs/NEXT_UPDATE_PLAN-2.2.md` §7) calls for a save time machine: automatic snapshots, retention, diff preview, and a restore as safe as backup restore. Snapshot metadata must stay out of `library.json` so the state file stays small at 20k games.

## Decision

- Snapshots live under `<data_dir>/save-vault/`: an atomic-write `index.json` plus `<game_key>/<UTC-timestamp>/snapshot.json` and mirrored save files. Each `snapshot.json` carries the per-file list `{relpath, sha256, size, mtime}` so a snapshot is self-describing even if the index is lost. Vault metadata is derived data-dir state, following the `media/manifest.json` precedent — not library state.
- Discovery reuses `scan_all_saves` (`pkg/parity/parity_saves.py`) and the `save_sources` provenance the Saves pane already uses; hashing reuses `file_sha256` (`saves.py`). Automatic triggers (`save_vault_on_close`, `save_vault_auto_days`) snapshot explicit `save_paths` and non-shared discovered saves (`shared:false`), excluding shared emulator-wide roots unless explicitly added in `save_paths`. Per-snapshot caps (`512 MiB`, `5,000` files) bound shared-root copies (`VAULT_LIMIT_EXCEEDED`). A game with no discovered saves returns `NO_SAVES` — a clean empty result, not an error.
- Path containment is adapted from `approved_media_path` (`pkg/state/media_probe.py`) plus `_reject_symlink_components` (`saves.py`): absolute paths, symlink-component rejection, and containment under the declared save roots. No snapshot or restore writes outside the vault dir or the game's save roots; escapes are refused with `VAULT_PATH_ESCAPE`.
- Retention keeps `save_vault_keep` snapshots per game (default 5, clamp 1–50), oldest pruned first. The mandatory pre-restore safety snapshot (`trigger:"pre-restore"`) is exempt from auto-prune but listed, so a botched restore is always recoverable.
- Triggers: manual (always available when saves exist), on-session-close (`save_vault_on_close`, default false, submitted beside the `backup_on_close` hook in `pkg/state/launch.py` without blocking session bookkeeping), and scheduled (`save_vault_auto_days`, 0=off, on the existing hourly tick). All run through `JOB_MANAGER` — cancellable, with progress.
- Restore is additive/overwrite by default; current files absent from the snapshot are deleted only when `include_removals:true` is passed explicitly. Restore previews a per-file diff (`added/changed/removed/unchanged`, the `saves.py` restore-preview vocabulary), supports `dry_run`, and reports per-file `{path, status, error?}` results.
- Settings: `save_vault_enabled` (true), `save_vault_on_close` (false), `save_vault_auto_days` (0), `save_vault_keep` (5) — added to `KNOWN_SETTINGS` in the same commit as their writers.
- The vault is excluded from backup archives: `BACKUP_ITEMS` (`pkg/parity/parity_backup.py`) already omits `save-vault/`. This is documented, not a new setting — snapshots are derived data that can be large; `docs/backups.md` records the decision and the manual-copy path.
- Routes are explicit `/api/v2/saves/vault*` operations (list, status, snapshot, diff, restore, delete). Problem codes: `VAULT_SNAPSHOT_MISSING`, `VAULT_GAME_MISMATCH`, `VAULT_PATH_ESCAPE`, `VAULT_LIMIT_EXCEEDED`, `VAULT_SAFETY_SNAPSHOT_FAILED`, `NO_SAVES`.

## Consequences

- Deleting `save-vault/` loses only snapshots, never live saves; the vault survives `library.json` deletion because it is data-dir state.
- Snapshots are plain file copies (no compression dependency), so disk cost scales with retention; byte accounting is visible in the Vault panel and the compat report, and `--check-library` reports vault size informationally.
- The Vault panel hooks the saves dialog (`openSaveHistory` in `static/sessions.js`), not the game-detail pane.

## Verification

Planned: `tests/test_save_vault.py` (CRUD, retention, index atomicity, malformed-index recovery, path escapes, size accounting), `tests/test_save_vault_http.py` (routes, auth, problem codes, job lifecycle), `tests/test_save_vault_faults.py` (crash mid-copy, disk-full, missing restore sources), plus a ui_smoke vault flow. Acceptance: plan §6-W1 and §7.10.