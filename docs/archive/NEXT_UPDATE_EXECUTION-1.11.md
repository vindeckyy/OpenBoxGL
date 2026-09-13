# Archived next-update execution checkpoint — 1.10.1 + 1.11.0 "Every Second Counts"

> **Completed progress ledger** for `docs/NEXT_UPDATE_PLAN.md`. Created
> 2026-09-12 at the M0c baseline gate and archived after M11 completion.
> Publishing and external submissions needed explicit authorization and were
> not performed.

## Milestone checkpoints

| Date (UTC) | Milestone | Commit | `make check` | Evidence / notes |
| --- | --- | --- | --- | --- |
| 2026-09-12 | M0 baseline (pre-implementation gate) | `02b3728` | PASS — exit 0 | Baseline evidence below; green before any lane opened. |
| 2026-09-12 | P1 — 1.10.1 sweep residuals (M2–M4) | uncommitted worktree (base `02b3728`) | PASS — exit 0 | Defects #11–#26 fixed + #27 regression suite; evidence below. |
| 2026-09-12 | M2 — T1 Quick Resume + Moments | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_resume.py`, `tests/test_resume_http.py`, `tests/test_moments.py`; adapter capability remains explicit. |
| 2026-09-12 | M3 — S2 recap + S3 trash + S5 memory roots | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_recap.py`, `tests/test_library_trash.py`, `tests/test_parity_memories.py`; bounded state and approved-media checks covered. |
| 2026-09-12 | M4 — T2 Library Time Machine | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_time_machine.py`, `tests/test_timemachine_http.py`; journal, as-of, and stale revert paths covered. |
| 2026-09-12 | M5 — T3 Record That | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_obs_bridge.py`, `tests/test_parity_reels.py`, `tests/test_clips_http.py`; OBS, fallback capture, and reel paths covered. |
| 2026-09-12 | M6 — T4 Backlog Radio + query + palette | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_radio.py`, `tests/test_parity_query.py`, frontend contract; deterministic chips and action search wired. |
| 2026-09-12 | M7 — T5 Arcade Room + Museum + kiosk | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_arcaderoom_contract.py`, `tests/test_arcade_http.py`; bounded canvas behavior and salted PIN boundary covered. |
| 2026-09-12 | M8 — S1 Steam Bridge + S4 SteamGridDB | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_steam_bridge.py`, `tests/test_steambridge_http.py`, `tests/test_parity_steamgrid.py`; VDF preservation and provider contracts covered. |
| 2026-09-12 | M9 — T6 Household + S6 trophies | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_household.py`, `tests/test_household_http.py`, `tests/test_parity_trophies.py`; opt-in stats and convergent records covered. |
| 2026-09-12 | M10 — S7 ES-DE + S8 polish | uncommitted worktree (base `02b3728`) | PASS — exit 0 | `tests/test_parity_esde_import.py`, `tests/test_launchbox_http.py`, i18n/frontend gates; preview/apply and discovery surfaces covered. |
| 2026-09-12 | M11 — integration, docs, packaging, RC | uncommitted worktree (base `02b3728`) | PASS — exit 0 | Final repository gate and version-sync evidence below; no commit, tag, publication, or external submission performed. |
| 2026-09-12 | M11R — resumed integration and performance close | uncommitted worktree (base `02b3728`) | PASS — exit 0 | Corrected final gate and regenerated `build/perf.json`; current evidence is recorded in the resumed checkpoint below. |

## M11 final gate evidence — 1.11.0 (2026-09-12)

`make check` completed with exit code 0 after the release-facing integration
pass:

- ruff: `All checks passed!`
- runtime modules: `123 entries, all exist, required globs covered`
- v1 route contract and version sync: pass; runtime/docs surfaces agree on
  `1.11.0`
- frontend: `eslint: OK`, `tsc: OK`
- i18n: all five locales at `100.0% (898/898 keys)`
- test suite: `130 test files passed, 0 failed`
- coverage: `83.0%` total (floor `72.0%`); `web_app.py` `59.0%` (floor
  `54.0%`)
- new runtime modules: `85%` (floor `85%`); changed-line coverage passed
- design tokens: `raw hex outside :root: 0 (baseline 0)`

The 1.11 source/docs update is ready for maintainer review. Release
publication, signing, tagging, and Flathub submission remain explicitly out
of scope.

## P1 evidence — 1.10.1 sweep residuals (2026-09-12)

Fixed defects (re-verified in-repo before each fix), all regression-covered by
`tests/test_sweep_residuals.py` (45 tests) plus one token-vocabulary pin update
in `tests/test_launch_tokens.py`:

- #11 `parity_launch_doctor.py`: naive/aware `expires_at` compare no longer
  raises `TypeError`; same bug fixed in `parity_setup_preview._is_expired`.
- #12 `saves.py`: restore recreates missing roots using the manifest-recorded
  root type; pre-restore safety backup uses `allow_empty=True` so a vanished
  save tree no longer aborts the restore.
- #13 `parity_import.detect_dependencies`: precedence fix — regular-file BIOS
  deps report `found=True`; unreadable dirs degrade to missing.
- #14 `parity_backup.diff_manifests`: compares `name` (and `title`) so renames
  register as changes.
- #15 `pkg/state/launch.py finish_session`: history entries carry int
  `exit_code` + `game_id`; legacy `[code, timed_out]` rows normalize on write,
  `game_id` backfilled only when the recorded name is unambiguous.
- #16 `parity_setup_preview.compute_summary`: media-gap check uses the real
  `cover`/`screenshots` fields, not `box_front`/`screenshot`.
- #17 `launch_tokens.py`: `{Path}` placeholder added (alias of `{path}`).
- #18 `parity_launchbox_import._find_target`: `target_game_id`/`source_id`
  identity wins; `target_index` only applies to identity-free operations.
- #19 `.m3u` writes: `parity_import._write_m3u_playlist` falls back to the
  generated dir (then first disc) on `OSError`; `parity_setup_preview` commit
  keeps the first-disc path when the staged write fails.
- #20 save/sync lock: shared `saves.SAVE_SYNC_LOCK` (reentrant) wraps
  `backup_saves`/`restore_saves` and `cloud_sync.sync_statistics`'s
  read-merge-upload section.
- #21/#22 `static/bigbox.js`: video-snap scheduling and screensaver gate on
  `game.has_video`; `has_cover` fallback preserved.
- #23 `handlers/media.py` EmuMovies: type→field map (snap→`screenshots`,
  box→`cover`, …), unknown types rejected, `bump_media_epoch()` after mutation.
- #24 `GET /api/media`: malformed `Range` now returns 416 + `Content-Range`
  (verified against the real server); unsatisfiable ranges already 416 in
  `send_file` — unchanged.
- #25 `static/reader.js`: page turns use `contentWindow.location.replace`
  (iframe src assignment polluted session history).
- #26 `static/app.css`: `--green` added to `:root` (all 5 themes already
  override it).

Gate: `make check` exit 0 — ruff clean, 108 test files passed / 0 failed,
coverage 82.0% (floor 72.0%), `web_app.py` 59.0% (floor 54.0%), tokens/i18n/v1
contract all green. Working-tree changed-line coverage on touched Python
modules: 100.0% (191/191 executable changed lines hit). Full log:
`/tmp/p1-make-check2.log`.

## Baseline evidence — M0 (2026-09-12)

`make check` at `02b37281aeb429d7a887e45654f1ea96630f4bc7` (`VERSION = "1.10.0"` in
`updates.py`), Python 3.14.7, dev dependencies in `.venv-dev`
(`coverage==7.16.0`, `ruff==0.16.5`):

- ruff: `All checks passed!`
- `runtime_modules.txt`: OK — 103 entries, all exist, required globs covered
- v1 route contract and version sync: pass
- frontend: `eslint: OK`, `tsc: OK`
- i18n: `PASS: all locale files have 100% key coverage` — 660/660 keys in each
  of en, es, de, fr, pt
- test suite: `107 test files passed, 0 failed` (serial under coverage)
- coverage: `82.0%` total (floor `72.0%`); `web_app.py` `59.0%` (floor `54.0%`)
- changed-line coverage: no Python changes since diff base — pass (floor 95%)
- new runtime modules since diff base: none
- design tokens: `raw hex outside :root: 0 (baseline 0)`
- Result: `GATE PASSED`, `make check` exit code 0.
- Full output: `/tmp/m0c-make-check-baseline.log`.

Baseline is green; LG1 (1.10.1 sweep residuals) and all feature lanes may build
on it. Working tree at gate time was clean except the untracked spec document
`docs/NEXT_UPDATE_PLAN.md` itself.

## Ledger rules

- The item that closes each milestone appends one checkpoint row above (date,
  milestone, commit, gate result, evidence pointer). Rows are append-only —
  corrections get a new row, never a rewrite.
- A red baseline or milestone gate is fixed or escalated before any further
  lane builds on it; a red gate is never documented around.
- `make check` must be green for each milestone close; evidence is real command
  output, not a paraphrase.
- The final documentation item audits that every milestone has a checkpoint and
  archives this file under the 1.11 name; it never creates the file.

## Post-M11 correction and reopen — 2026-09-12

The M11 row and the M11 evidence section above were recorded before a fresh
release-candidate audit. They remain unchanged as append-only historical
entries, but they are not current completion evidence. This resume pass
reopened integration after finding that the recorded gate did not cover the
latest worktree state.

The reopened audit identified and corrected five issues: resume retention could
follow an archive symlink, verified sync identity lineage was omitted from
snapshots, OBS replay teardown released its lock before stopping OBS, same-title
Steam shortcuts could overwrite one another, and Household winner ordering
could discard record timestamps. The current worktree also added the missing
Household, Arcade, Recap, and Moments locale coverage.

The first fresh performance sample on the pre-optimization handler failed the
write-path thresholds at 10k/20k fixtures; the local-only mutation fast path was
then added and the direct write-only measurements passed at approximately
265 ms and 518 ms. The existing `build/perf.json` artifact is not treated as
current evidence until the full benchmark is regenerated from the final tree.

No physical handheld, native WebKitGTK walkthrough, or external ES-DE device
exchange was performed in this resume pass. Those environments remain
unverified and must not be described as release-tested.

The final checkpoint below remains pending until the current tree passes the
full repository gate and the latest performance artifact is regenerated.

## Resumed final checkpoint — 1.11.0 (2026-09-12)

The resume pass is complete on the current uncommitted worktree. The earlier
M11 row above remains historical and was not rewritten; this checkpoint is the
current release evidence. The pending status sentence in the preceding
correction section is superseded by this completed checkpoint.

`make check` completed with exit code 0 after the final integration and
performance-path correction:

- ruff: `All checks passed!`
- runtime modules: `123 entries, all exist, required globs covered`
- v1 route contract and version sync: pass
- frontend: `eslint: OK`, `tsc: OK`
- i18n: all five locales at `100.0% (1005/1005 keys)`
- test suite: `130 test files passed, 0 failed`
- coverage: `83.0%` total (floor `72.0%`); `web_app.py` `59.0%` (floor
  `54.0%`); new runtime modules `85%` (floor `85%`); changed-line coverage
  passed
- design tokens: `raw hex outside :root: 0 (baseline 0)`

The regenerated benchmark also passed:

```text
python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5
GATE PASSED: performance non-regression gates satisfied
```

Final p95 measurements from `build/perf.json` were 262.3 ms (10k write),
537.6 ms (20k write), 67.5/135.2 ms (10k/20k picker), and 778.8/611.1 ms
(10k/20k constellation). The constellation path now uses the cached read-only
state view, avoiding a full catalog deep copy on every graph request.

The final audit also added regression coverage for resume symlink containment,
failed archival preservation, verified sync identity snapshots, serialized OBS
replay teardown, same-title Steam identities, Household winner timestamps,
canonical executable localized query examples, and the expanded OBS protocol
error paths. No commit, tag, publication, signing, or external submission was
performed. No physical handheld, native WebKitGTK walkthrough, or external
ES-DE device exchange was performed; those environments remain unverified.
