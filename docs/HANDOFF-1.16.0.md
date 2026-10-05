# Handoff — OpenBox 1.16.0 release execution

> Written by MCode at the maintainer's request, for a Claude session picking this work up.
> **Everything below was verified by direct inspection at the time of writing** (see
> "Verified state" for the exact commands and their output). Nothing is inherited from the
> previous session's notes without a check.
>
> Working tree: branch `release/1.16.0`, **83 changed/new files, nothing committed.**

## Status after execution (2026-10-05)

Everything below has been executed. Sections 0–5 are kept as the record of what was found;
read this block for the current state.

- **§0 A, B, C fixed.** `session_event` takes `timed_out`; the `session.stopped` webhook
  allowlist carries it too. `StoppedEventPayloadTests` in `test_playtime_attribution.py`
  reads both serialized payloads as JSON (the MagicMock stubs had hidden the bug). The F2 row
  is row 50 in a new `## 1.16.0` section of `reliability.md`. 55 i18n keys × 5 locales added
  with the validated writer (the files carry no trailing newline; the writer matches).
- **§3 corrections.** S27 was *not* fixed: `setup.js` still posted `review` as `import`. It now
  posts nothing for a review candidate and the Confirm step counts them. `defs.js:37` no longer
  uses `escapeHtml` (the handoff note was stale). `test_frontend_contract.py` has 65 tests, not
  73. All 14 §3.2 items and both "unverified" items are done.
- **§4 F1 shipped:** `pkg/parity/parity_launch_audit.py`, `GET /api/v2/launch/audit`,
  `POST /api/v2/launch/audit/scan` (two routes, not one: GET must not start a job), panel in the
  Library health dialog, `tests/test_launch_audit.py` (13), `ui_smoke motion.launchAudit`.
  28 spawns for 2,000 games vs 4,000 without the cache. Cut per the plan's order: card-level
  surfacing, and fixes beyond "Install emulator".
- **§5 done:** ADR 0064, reliability rows 51–54, version 1.16.0 on every surface
  (`check_version_sync.py` OK), SECURITY.md leads with the security class and drops 1.15.x
  (matches its existing no-backport policy — confirm), migration note, Corrections to 1.15.0,
  full carry-forward table, `api-v2.md` regenerated, routes baseline grown by 2.
  Metainfo screenshot URLs stay on `v1.15.0` (flathub-checklist invariant).
- Also fixed on the way: `_flatpak_installed` did not catch its own timeout; a job-manager race
  where a finished job's id still resolved to its name (and a vacuous `or True` assertion in its
  test); 27 ruff errors; `state_store.py` import order; and (from the ultrareview) the
  auto-import worker indexing `merge_imported_games`'s `(added, found)` tuple with `["added"]`,
  a TypeError that killed the thread on the first storefront tick. Its tests mocked the merge
  with a dict; they now use the real shape, plus one test through the real merge.
- **Commit note:** `pkg/parity/parity_launch_audit.py`, `tests/test_launch_audit.py`,
  `scripts/check_dependencies.py`, `docs/adr/0064-…`, `docs/archive/release-notes-1.15.0.md`
  and the other new files are untracked; the ultrareview flagged two as missing because the
  bundle only carried tracked files. `git add` them with the rest.
- `tools_dom.py` / `tools_scan.py` were moved to the session scratchpad, not deleted.
- The What's New dialog showed 1.14.0 highlights under a live "1.16.0" title. It now renders
  six 1.16.0 cards from a `HIGHLIGHTS` list in `whatsnew.js` (all five locales); the 12 stale
  card keys were removed.

---

## 0. Read this first — three blockers, not one

`python scripts\run_windows_tests.py` → **166/174 passed, 8 files failed.** The previous
session reported its Phase 7b work as done, but it only ever ran the tests it had
written itself. The full suite says otherwise. There are **three independent root
causes**, and the first is serious:

| # | Root cause | Failing files | Severity |
|---|---|---|---|
| **A** | **S26 regression — `finish_session` raises `TypeError` on every session stop** | `test_sessions`, `test_session_persistence`, `test_launch_phases`, `test_recap`, `test_changelog_features` | **High — see §0.1** |
| **B** | **All i18n work lost to a NUL-byte corruption** | `test_i18n`, `test_frontend_contract` | Medium — see §0.2 |
| **C** | **`docs/reliability.md` has a duplicate row 24** | `test_feature_contracts` | Low — see §0.3 |

**Suggested order of work** (each step is independently verifiable, and the suite gets
greener after every one):

1. **Root cause A** (§0.1) — one signature change, unblocks 5 of the 8 failing files, and
   is the only one where the *product* is currently wrong at runtime. Do this first.
2. **Root cause C** (§0.3) — renumber one row to 50. Trivial, and it clears
   `test_feature_contracts`, which is the gate that guards the reliability catalog you
   will need for Phase 9.
3. **Root cause B** (§0.2, §0.4) — 54 keys × 5 locales. Mechanical but large, and the
   only step with a real risk of silent corruption, so do it with the validated writer
   in §0.4 and re-verify with `check_i18n.py`.
4. Confirm green: `python scripts\run_windows_tests.py`.
5. Then the real remaining work: **Phase 7b §3.2** (15 items) → **Phase 8** (F1) →
   **Phase 9** (release engineering).

### 0.1 Root cause A — S26 did not actually work, and it broke session finalization

`pkg/state/launch.py:1028` passes a keyword the callee does not accept:

```python
# pkg/state/sse.py:288
def session_event(kind, launch_id, game_name, exit_code=None, seconds=None):
                                      # <-- no timed_out parameter
```

```python
# pkg/state/launch.py:1028
sess_ev("stopped", launch_id, game_name, exit_code=settled_exit_code,
        timed_out=settled_timed_out, seconds=seconds)
# TypeError: session_event() got an unexpected keyword argument 'timed_out'
```

This is worse than a failing test, for two reasons:

1. **`finish_session` runs in a watcher thread** (`Thread-1`), so the `TypeError` is
   raised in a background thread, logged, and swallowed. The tests see it only because
   they call `finish_session` directly on the main thread.
2. **It kills the very event S26 was written to fix.** Line 1028 raises, so **line 1029
   never runs** — and line 1029 is the `pub_sess_ev(build_event("session.stopped", …))`
   call that actually ships the scalar `exit_code` to the client. The S26 client fix in
   `static/sessions.js` is therefore **dead code**; the trust bug it was meant to close is
   still open in production.

**Fix:** add `timed_out=False` to `session_event`'s signature in `pkg/state/sse.py` and
propagate it into the event payload, **or** drop the keyword from the `sess_ev` call if
`session_event` is purely the internal `SESSION_EVENTS` registry path that does not need
it. Read `sse.py:288-320` first and decide which — do not just silence the `TypeError`.
Then confirm the `session.stopped` payload really carries `"exit_code": <int>` and
`"timed_out": <bool>` (not an array) end-to-end, because the plan's S26 acceptance
criterion is *"the exit code crosses the SSE boundary as a scalar, and the 'Session
failed' branch is reachable."* A unit test asserting the **serialized** payload shape
would have caught this and should be added.

### 0.2 Root cause B — the i18n data loss

### 0.5 The 54 missing keys, in two groups

**Group A — 29 `_one` singular variants** demanded by `test_counted_strings_have_a_singular_form`.
The gate flags any key whose text contains `{count}`, that is not itself a `_one`/`_many`
key, and that has no `_one` variant and no exemption. Current English values (these are
the *plural* forms; each needs a natural singular sibling):

```
artwork_doctor.fixed                          "Fixed artwork for {count} entries."
artwork_doctor.last_batch                     "Last batch {batch} · {count} files"   (· is U+00B7)
artwork_doctor.summary                        "Scanned {count} games."
artwork_doctor.undone                         "Restored {count} artwork entries."
defs.rolled_back                              "Removed {count} definitions the channel installed."
defs.status_pack                              "Community pack {version} installed ({count} definitions)."
dialog.media_show_more                        "Show {count} more media types"
duplicates.absorbed                           "{count} entries will move to the trash (undoable)"
duplicates.changes                            "{count} fields will be filled from duplicates"
duplicates.merged                             "Merged {count} duplicate entries"
duplicates.summary                            "{count} duplicate groups"
health.computed                               "{count} games scored"
health.preview_matches                        "{count} unambiguous path matches will be relinked."
health.preview_merges                         "{count} duplicate groups will be merged; absorbed entries move to the Trash."
health.preview_targets                        "{count} artwork targets will be fetched with the Artwork Doctor."
library.games_count                           "{count} games"
metadata.esde_found                           "{count} source games"
metadata.launchbox_found                      "{count} source games"
moments.count                                 "{count} moments"
moments.open_card                             "Open {count} moments"
repair.ambiguous                              "{count} need a manual pick"
repair.applied                                "Relinked {count} paths"
repair.matches                                "{count} matches"
repair.skipped                                "{count} paths changed since the scan and were skipped"
repair.summary                                "{count} missing paths"
settings.library_sync_applied                 "Applied {count} catalog changes."
settings.library_sync_conflicts_remaining     "{count} conflicts remain; choose a value for each one."
settings.library_sync_published               "Published {count} local catalog events."
trash.emptied                                 "Trash emptied ({count} entries)."
```

Note `repair.ambiguous` is a **verb-agreement** case: `1 need a manual pick` is wrong;
count=1 must read something like `1 needs a manual pick`. The gate only checks key
presence, but the release note claims the strings are correct, so write it properly.

The exemption list `COUNT_NEEDS_NO_SINGULAR` in `tests/test_frontend_contract.py:494`
already covers 15 other counted keys (adjectives, ellipticals, prepositional phrases)
and has a self-policing test. **Do not add to it** — `COUNT_NO_SINGULAR_CEILING = 15`
is already exactly met, and growing it fails the gate. Read the reasons in that dict
before deciding any of the 29 above belongs there instead of getting a `_one`.

**Group B — 25 keys referenced in code but absent from `en.json`** (from `check_i18n.py`).
These are the F2/F10/S50/S51 strings:

```
backup.preview_cancel          backup.preview_retry
backup.preview_consequence     backup.preview_secrets_kept
backup.preview_failed          backup.preview_settings
backup.preview_loading         backup.preview_settings_changed
backup.preview_none            backup.preview_settings_missing
backup.preview_showing         backup.preview_title
backup.preview_will_add        backup.preview_will_change
backup.preview_will_remove     library.query_truncated
metadata.launchbox_more        metadata.launchbox_confirm_title
metadata.launchbox_confirm_message    metadata.launchbox_confirm_consequence
metadata.launchbox_confirm_button     metadata.esde_confirm_title
metadata.esde_confirm_message         metadata.esde_confirm_consequence
metadata.esde_confirm_button
```

(`backup.preview_changes` already exists in `en.json` as "Preview changes" — do not
re-add it.) Placeholders, from the call sites in `static/settings.js` /
`static/library.js` / `static/imports.js`:

| key | placeholders | notes |
|---|---|---|
| `backup.preview_showing` | `{shown}`, `{total}` | e.g. "Showing {shown} of {total}" |
| `backup.preview_settings_changed` | `{count}` | **carries a count → also needs `_one`** |
| `library.query_truncated` | `{shown}`, `{total}` | F10 |
| `metadata.launchbox_more` | `{shown}`, `{total}` | shared by both import panels |
| `metadata.launchbox_confirm_consequence` | `{total}`, `{added}`, `{merged}` | **carries counts** |
| `metadata.esde_confirm_consequence` | `{total}`, `{added}`, `{merged}` | **carries counts** |

`scripts/check_i18n.py` gained a **placeholder-parity** rule this session
(`_placeholder_drift`), so a translation that drops `{count}` fails the gate. It
checks both directions and is order-insensitive.

An earlier session authored **54 i18n keys × 5 locales** by writing JSON with
`path.write_text(encoding="utf-8")`, and on Windows that silently produced
**NUL-byte files**. The corruption was repaired with `git checkout -- locales/`,
which was correct as an emergency action and **destroyed all the translation work**
in the process. The *code* that consumes those keys is all in place; only the
locale data is gone.

Concretely, right now:

```
FAIL test_counted_strings_have_a_singular_form   (29 keys)
FAIL test_the_singular_exemption_list_is_still_justified  (same 29 keys)
python scripts\check_i18n.py  →  FAIL: 1 error(s)
      25 keys referenced in code but missing from en.json
```

All 5 locales are at the **1240-key baseline** (NUL-free, key sets identical,
100% coverage) — i.e. the tree is clean, it is just the *new* keys that are absent.

### 0.3 Root cause C — `docs/reliability.md` row numbering

`test_feature_contracts.py` asserts the reliability catalog's row numbers are
contiguous from 1. The F2 row added in Phase 5 reused **24**, which already existed:

```
1 … 23, 24, 24, 25 … 49     → 50 rows, 24 appears twice, 50 does not exist
gate: "row numbers are not contiguous from 1 (found 50 rows, missing [50])"
```

**Fix:** renumber the F2 row to **50** (the new highest row), not by shifting rows
25–49 — those are existing, reviewed rows and moving them churns the diff for no
reason. The gate wants `1..N` with no gaps and no repeats, so one new row at the end
is the minimal correct change.

### 0.4 How to write the locale files without repeating the corruption

The failure was silent and destructive. Do it this way instead:

```python
# write a .py file and run it -- do NOT inline this in PowerShell
import json, io, pathlib
for loc in ("en", "es", "de", "fr", "pt"):
    p = pathlib.Path("locales") / f"{loc}.json"
    data = json.loads(p.read_text(encoding="utf-8"))     # parse first
    ...mutate nested dicts...
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    p.write_text(text, encoding="utf-8", newline="\n")   # explicit newline
    raw = p.read_bytes()
    assert b"\x00" not in raw, f"{loc}: NUL bytes"
    json.loads(raw.decode("utf-8"))                      # re-parse
```

Two traps, both learned the hard way:
- **Always `json.loads()` after writing, before moving on.** 270 entries is a lot to
  lose to a silent write.
- **`ensure_ascii=False` is required** — the tree contains `·` (U+00B7) and other
  non-ASCII. If you write with `ensure_ascii=True` you get a semantically-equal but
  enormous diff. If you write with `ensure_ascii=False` you must validate, because
  that is the mode that corrupted the files.
- `artwork_doctor.last_batch` contains a middle dot; confirm it survived as `\xb7`
  and did not become `?` or `\ufffd`.

---

## 1. The plan

`docs/NEXT_UPDATE_PLAN-1.16.md` (1,184 lines, 26 sections). Renamed from
`NEXT_UPDATE_PLAN-1.15.1.md`. Gitignored on purpose (`.gitignore:54`) — it is a
working document, not a shipped artifact.

**Do not renumber finding IDs.** The document reads S1–S17 → S26–S54 → S18–S25
because §2.5b was appended after §2.6; ~40 cross-references depend on it. Search by ID.

**The release shape (decided, do not relitigate):** one minor bump `1.15.0 → 1.16.0`,
no patch/minor split. A minor bump is the *correct* SemVer label because the release
adds a route and two features. Phase order in §7 is binding — it is the risk control,
not the version number.

---

## 2. What is done (phases 0–7 complete)

### Phase 0 — Baseline & instrument
S20 i18n placeholder-parity gate (`_placeholder_drift` in `check_i18n.py` + 3 tests in
`test_i18n.py`) · S21 `--dur-loop` completeness gate · S22 single motion owner
(`motionMs()` is now the only motion read; gate is AND not OR) · migrations of
`bigbox.js` + `arcaderoom.js` off raw `matchMedia` · D1–D7 docs · ADR 0017, 0018.

### Phase 1 — Security
| | Fix | Test |
|---|---|---|
| S1 | `orjson` branch deleted from `state_store.py`; NEW `scripts/check_dependencies.py` gate with a `REVIEWED_OPTIONAL` allowlist (yaml, py7zr); wired into `check_tests.py`, `Makefile ratchets`, `ci.yml` | `test_dependencies_gate.py` (10), `test_ci_gates.py` (9) |
| S1b | `escapeHtml` on all 4 interpolations in `wrapped.js` | sink-based gates in `test_frontend_contract.py` |
| S2/S3 | `SAFE_IDENTIFIER_RE`, `safe_identifier`, `contained_path`, `safe_filename_component`, `resolved_data_dir` in `pkg/platform_compat.py`; 3 call sites fixed | `test_path_traversal.py` (14) |
| S4 | sandbox masks the **resolved** data dir (`_needs_data_dir_mask`) | `test_plugin_sandbox.py` (12) |
| S5 | `int(console["ID"])` in `retroachievements.py` | — |
| S6 | `FLATPAK_TIMEOUT_SECONDS = 5` on both flatpak calls | — |

### Phase 2 — Data integrity
S7 `-1` → `None` sentinel (**proved the old code credited 7500 s to the wrong game**;
`test_playtime_attribution.py` 18) · S8 `.m3u` clobber + stale-sheet parse
(`test_import_playlist_safety.py` 14) · S9 cloud sync re-merges inside the mutator
(`test_cloud_sync_concurrency.py` 12) · S10 two-phase commit with `_stage` /
`_backup_name` / `_replace_with_retry` / `_undo_install` (`test_defs_install_atomicity.py`
15, and all 42 pre-existing defs tests still pass) · S11 superseded job is cancelled +
archived + notified **outside** the lock, `_names_by_id.pop` in `finally`
(`test_job_replace_cancel.py` 11, all 84 pre-existing job tests pass).

### Phase 3 — Concurrency
S12/S13 all readers take `self._lock`, superseded rebuild no-ops, `_generation` counter
(`test_readmodel_concurrency.py` 11) · S14 `_platform_category_fingerprint` (blake2b over
sorted categories) replaces `id(settings)` in the cache key
(`test_platform_category_cache.py` 11) · S15 `_applier_script` try/catch + guarded restore
+ `exit 1`, stale scratch sweep (`test_windows_update_rollback.py` 11) · S16 `_state_digest`
+ skip unchanged writes (`test_noop_state_writes.py` 16) · S17 `_REGISTRY_LOCK` RLock,
registry published complete (`test_registry_concurrency.py` 8).
**Snapshot-ordering flake FIXED** — names are now
`%Y%m%dT%H%M%S%fZ-<8-digit seq>-<hex>.json` and `snapshots()` sorts by parsed name
(hard links share `st_mtime`, so mtime cannot order them). 0 failures in 40 runs;
9 tests in `test_state_snapshots.py`.

### Phase 4 — The 1.15.0 false claims
B1 delegated **bubble-phase** `cancel` listener honouring `defaultPrevented` + delegated
backdrop handler; 3 raw `.close()` cancel handlers migrated; 3 files gained
`import { closeDialog }` · B2–B5 a real toast manager in `state.js`
(`TOAST_LIMIT=3`, `toastEntries` Map, `armTimer`/`pauseTimer`/`resumeTimer` with real
pause, `enforceLimit`, `raiseToasts`), `#toasts` is a `popover="manual"` container, toasts
stack by **explicit `bottom` offsets** (a flex column cost 0.0017 CLS per append — now 0),
3 `innerHTML` writers migrated. Gates: `test_every_dialog_close_path_goes_through_the_exit_choke_point`,
`test_escape_is_not_handled_twice`, `test_the_toast_surface_has_one_writer`,
`test_toast_timers_pause_and_resume`, `test_toasts_are_raised_above_dialogs`.

### Phase 5 — F2 Restore Preview
`parity_backup.diff_manifests` fully rewritten around `restore.will_remove/will_add/will_change`
(names relative to the **restore**, not the library), per-field `{from, to}`,
`DIFF_PREVIEW_LIMIT=200`, honest `total` + `truncated`, and the
`settings.restored/present/would_change/differs` distinction. 8 new functions in
`settings.js`; the restore button is built **only** by `renderRestorePreview` and armed
only by `commitRestore` under a `backupPreviewPath` guard. `test_restore_preview.py` (11),
3 gates, `ui_smoke motion.restorePreview` (12 assertions, 0 restore POSTs during preview),
`docs/reliability.md` row 24.

### Phase 6 — Overlays, panels, dialogs
S18 `inert` with `INERT_KEEP_VISIBLE` + a `MutationObserver` on `#bigBox.hidden` that
releases inert on **any** path that hides Big Box (a real regression, found by ui_smoke) ·
S19 `pluralVariant` mechanism in `i18n.js` + the two gates **← data missing, §0.1** ·
S23 `motionMs('--dur-base') + 50` replacing the 1500 ms literal · S24 `primaryControl()` ·
S25 removed the vacuous `if errors:` guard.

### Phase 7 — Frontend correctness F1–F14
F1 `_refreshCounter++` · F2 `HTTP_URL_RE` once at the top of `nativeOpenExternal` ·
F3 `lastMeasuredRowHeight` survives the reset, fallback is the *measured* height not
"render all" (20 k rows → 18 rendered, verified in a real browser) · F4 revision bump only
on a **bucket change** · F5 `beginDetailsRequest`/`isCurrentDetailsRequest` +
`AbortSignal` into `loadDoctor`/`loadRelated` · F6 `focusGridFallback` ·
F7/F11/F12 `QUERY_TOKEN_RE` keeps quoted phrases together, drops empty tokens, unknown key
→ `false` · F8 window-level drag/drop guard · F9 `api()` has `AbortController` +
`API_TIMEOUT_MS = 60000` + caller-signal passthrough · F10 `match_count` surfaced via
`queryMatchTotal`/`queryMatchTruncated` · F13/F14 selection and context menu re-render.

---

## 3. What is in progress — Phase 7b (trust bugs S26–S54)

### 3.1 Done

| | Fix | File |
|---|---|---|
| S26 | ⛔ **BROKEN — see §0.1.** `exit_code` is normalized to a scalar at `launch.py:1026`, but the `sess_ev(...)` call on the next line passes `timed_out=` to a `session_event()` that has no such parameter. The `TypeError` is raised in the watcher thread and **skips the `session.stopped` publish on line 1029**, so the client never receives the scalar. The `static/sessions.js` client fix is correct but unreachable. | `pkg/state/launch.py:1028`, `pkg/state/sse.py:288`, `static/sessions.js` |
| S27 | ✅ (see note) | `static/setup.js` |
| S28 | `gameForSession()` is now the canonical session→game lookup: `stable_game_id`, then `session.game_id` as row id, as array index, then as a string stable id. The old `item.id === session.game_id` pattern compared a **DB row id to an array index** | `static/state.js`, used by `sessions.js` + `bigbox.js` |
| S29 | staleness guard moved **before** `resultRows = rankRecent(...)` | `static/palette.js` |
| S30 | `clearVideoSnap()` on every close path (video looped forever, BGM stuck at 0.1) | `static/bigbox.js` |
| S31 | `visibilitychange` + `focus`/`blur` + `gamepadconnected`/`disconnected` all call `syncGamepadLoop()` — **no `focus`/`blur` listener existed anywhere in `static/`**, so alt-tab killed gamepad input permanently | `static/gamepad.js` |
| S32 | zero-length guard before the modulo | `static/bigbox.js` |
| S33 | try/catch around the session-control await | `static/bigbox.js` |
| S38 | both `postDecisions` and `runPreflightBatch` wrapped; failure deletes the choice and re-renders instead of committing a discarded one | `static/setup.js` |
| S40 | single `scheduleSessionPoll`; the `finally` schedules, the busy branch only yields (the busy branch **and** the finally each scheduled → permanent fork chains) | `static/sessions.js` |
| S45 | cursor-paginated walk of the whole job queue (polling `?limit=100` + `find` gave a false "Timed out" on a job that succeeded) | `static/setup.js` |
| S46 | failure clears both `launch_setup` and `flatpak_app_id` | `static/setup.js` |
| S50 | `confirmAction({destructive: true, ...})` with exact counts on both apply paths | `static/imports.js` |
| S51 | "showing N of T" overflow line in both report renderers | `static/imports.js` |

⚠️ **S27 is marked done but was not verified after the locale restore** — re-read
`static/setup.js:162` and `selectedImportCandidates()` (`:231`) and confirm the
`review` action is neither coerced to `import` at commit nor excluded from the count.

### 3.2 Not done — 15 items, all `static/`, all small

I read the call sites for each; these are the precise shapes:

| | File:line | What is wrong | Fix shape |
|---|---|---|---|
| S34 | `constellation.js:267` | `if (instant) tick(); else …rAF(tick)` — the **reduced-motion** path recurses *synchronously*, ~259 iterations × the O(n²) repulsion loop at 400 nodes ≈ **20.7 M pair computations in one blocking task** (129.5 M at the 1000 cap). `stopSim` cannot interrupt synchronous recursion, so the *accessibility* path is the one that freezes | convert the instant path to rAF-chunked convergence (or a bounded iteration count with an explicit comment); it must remain non-animated |
| S35 | `constellation.js:176` | `catch(error) { console.error(error) }` — spinner stays up forever | surface the error, clear `$('constellationLoading')` |
| S36 | `mood.js:104-107`, `:144` | the 3 s timeout is cleared **before** `processImage` runs, and its `throw` (no usable pixels) escapes into a promise nobody observes — via `decode()` swallowed by a trailing `.catch(() => {})`, via `onload` an uncaught handler error. The mood pipeline **hangs permanently** and leaks a pending promise per render | settle once (`settled` guard), route `processImage`'s throw into `reject`, keep the timeout until a terminal state |
| S37 | `metadata.js:275`, `:304` | `bulkAcceptClass` and `applyMatchReview` have no try/catch and both onclick handlers discard the promise. `handlers/metadata.py:704-708` raises inside the per-item loop → **one stale `game_id` atomically rejects all 200 accepts** and the button looks dead | wrap both; report the partial failure honestly |
| S39 | `arcaderoom.js:1009-1019` | `await callback(...)` then an unconditional `this.close()`. `sessions.launch` resolves normally on **every** failure path, so cancelling a `launch_confirm` dialog still closes the room and launches nothing | only `close()` when the callback signals it actually launched; otherwise keep the room open |
| S42 | `timeline.js:23` | `media(entry.game_id, 'cover')` passes a scalar where `media` reads `game.id` → **every timeline cover requests `/api/media?id=undefined`**. The only one of ~10 call sites that does this | pass `{ id: entry.game_id }` or use the correct form |
| S43 | `metadata.js:234-245` | `waitForPreviewReady` polls 120×800 ms with no cancellation and writes **shared** `reviewState.revision` each pass; two overlapping reviews make "Apply accepted" POST preview B with preview A's revision | generation guard (same shape as F5's `beginDetailsRequest`) |
| S44 | `timemachine.js:89` | `{once: true}` on `#tmLoadMore` plus the `loadingEvents` guard: a double-click consumes the listener without loading, **permanently killing pagination** until the dialog reopens | drop `{once: true}`; let the guard protect the handler |
| S47 | `defs.js:23` | `say(escapeHtml(...))` where `say()` assigns `textContent` → **double-escaped**; users literally see `&lt;script&gt;` | drop `escapeHtml` (textContent is the sanitizer) — *note `defs.js:37` correctly uses `escapeHtml` inside an i18n param, which is a different sink; leave that alone* |
| S48 | `dna.js:40` | `.catch(() => {})` on the settings persist — the toggle appears to apply, then silently reverts on restart | surface the failure, or roll the UI back |
| S49 | `insights.js:271` + `:301-303` | `ensurePanelHeader(true)` on every `localechange` re-runs `addEventListener` → **N duplicate `app:show-game` dispatches** per click after N language switches | make the panel listener bind once (guard it, like `bindInsights` already does for the IntersectionObserver) |
| S52 | `mastery.js:85-87` | dispatches `app:show-game` with `gameId: null` and writes `AppState.platformFilter`, which is **not an AppState key** (the real one is `platform`, `state.js:20`) — the whole interaction does nothing | write `AppState.platform`; decide what the click should actually do and make it do that |
| S53 | `storefront.js:70-79` | `watchGameyfinInstall` polls 1200×1500 ms with **no cancel path** (30 min) | accept an `AbortSignal`; wire it to dialog close |
| S54 | `arcaderoom.js:1062-1077` | no client-side backoff on the museum-PIN prompt — every 90 s idle period re-prompts indefinitely | exponential/interval backoff after a failed verify |

**S41** (`wrapped.js:28`, unescaped `e.message`) was folded into the S1b sink rule and
is fixed by the existing `test_unescaped_untrusted_data_never_reaches_an_html_sink` gate.

Two items from the plan's **"Unverified, stated rather than claimed"** paragraph are
also still open and are worth closing while you are in these files:
- `metadata.js:561` passes provider artwork URLs into `media_urls` with no `http(s)`
  scheme check, where `defs.js:fact()` validates.
- `constellation.js:311` `node.name.slice(0,1)` would throw inside the rAF callback and
  permanently kill the sim (the next frame is scheduled *after* `draw()`) if any node
  can lack a `name`.

---

## 4. Phase 8 — F1 Launch Audit (not started)

Plan §3. This is the headline feature. The design is already fully specified in the
plan; do not re-derive it. The load-bearing constraint:

> `_flatpak_installed` and `_flatpak_fs_allowed` each spawn a subprocess **per game**,
> uncached. A 20,000-game audit is up to **40,000 spawns** — over an hour.
> **Memoize the right thing:** `_flatpak_fs_allowed`'s *answer* depends on `rom_path`
> (it walks permission lines to check path grants), so you **cannot** cache the boolean
> by `app_id`. **Cache the permission string per `app_id`** — 24 definitions ⇒ ≤24
> distinct app_ids — and evaluate the path grant per ROM cheaply. That is ≤48 spawns
> (2 × 24). `which("flatpak")` and `which(native_exe)` cache the same way.
> **Reuse the cached-probe discipline already in `parity_library_health.py:_default_probe`;
> do not invent a second cache.** Zip opening stays an opt-in (deep mode, default off).

Shape: mirror `handlers/library_health.py` exactly —
`SCAN_JOB_NAME`/`run_health_scan`/`submit(replace=True)` → `launch-audit`/`run_launch_audit`;
cached `health_snapshot` → `audit_snapshot`; paginated `health_issues` → `audit_issues`
grouped by Doctor `code`; `health_fix`+undo → `audit_fix`/`audit_undo`. **One** new
route, `GET /api/v2/launch/audit`. `ctx.progress(...)` and `ctx.is_cancelled` exist.

Hard rules: reuse `run_preflight_checks` (never a second taxonomy); the audit calls it
**inside the job** so `preflight_batch`'s 200-item cap is irrelevant; progress + cancel
are mandatory; results are advisory and every fix is dry-run-by-default with an undo
token. Reuse the Doctor's existing `fix_action` payloads — do not invent a fix DSL.

**The gate asserts the spawn count, not the wall clock** — ≤48 flatpak spawns and ≤24
`which` misses for 2,000 games, using a counting fake `run`. Deterministic and CI-safe.
Add `launch_audit_ms_p95` to the `PERF.md` budgets table in the existing
`facet_ms_p95`/`picker_score_ms_p95` style. Cut order if time runs out: deep mode →
card-level surfacing → fix paths (keep the report).

---

## 5. Phase 9 — Release engineering (not started)

1. **ADR 0064** — `docs/adr/0064-changelog-truth-contract.md`. Four rules:
   (1) a claim is only written after the gate that proves it exists, or it carries an
   explicit `Manual`/`Documented` status in `docs/reliability.md`; (2) gates test
   behavior, not presence; (3) a gate that cannot fail is removed or fixed (S25);
   (4) a shipped claim is re-verified at the next release against the code, not the plan.
2. **`docs/reliability.md`** — add `Tested` rows naming tests that **exist** for F1 (×2),
   F2 (row 24 already added), S7, S8. The catalog rejects a phantom row.
3. **Version bump `1.15.0 → 1.16.0`** across all 14 surfaces, gated by
   `scripts/check_version_sync.py`: `updates.py` `VERSION`; `README.md` (badge,
   "Latest stable", installer `VERSION=` ×2); `openbox.metainfo.xml` (`<release>` +
   screenshot URLs); `docs/PARITY.md`; `docs/SUPPORT.md`; `docs/SECURITY.md`;
   `docs/flathub-checklist.md`; `docs/README.md`; `.github/ISSUE_TEMPLATE/bug_report.yml`;
   `docs/CHANGELOG.md` `## [1.16.0]`; `docs/RELEASE_NOTES.md`.
4. **`docs/SECURITY.md` supported-versions line** — decide whether `1.15.x` stays
   supported alongside `1.16.x`. This is a **policy call for the maintainer**, and the
   one genuinely open decision in the release. Lead that section with the §2.2 security
   class so a user on 1.15.x learns what they are exposed to before reading anything else.
5. **A migration note in the release notes**, because §2.3 changes *observable* behavior.
   Users will otherwise read these as regressions:
   - a failed launch now **says** it failed (S26);
   - a `review` import candidate is no longer silently coerced to `import` (S27);
   - a session card no longer shows the wrong game's extras (S28);
   - a foreign `.m3u` is no longer overwritten (S8).
6. **"Corrections to 1.15.0"** — four claims shipped as documentation and not as
   behavior. The notes must correct them by name: dialog exits on Esc, the toast queue,
   the defs channel's atomicity, the reduced-motion gate. A release that silently fixes
   a documented behavior leaves the changelog history wrong forever.
7. **Full carry-forward table** in the notes (D6 named 2 of 11 deferred items).
8. Regenerate `docs/api-v2.md` — `gen_api_docs.py --v2 --check` is already wired into
   `check_tests.py`, so a stale doc fails the gate. **Do not touch the routes contract
   baseline** — F1's new route makes it *grow*, which ADR 0060 permits freely.

---

## 6. House rules — the constraints that will bite you

- **Runtime is stdlib-only.** `pyproject.toml` and `requirements-dev.txt` mandate zero
  third-party dependencies. `scripts/check_dependencies.py` enforces it; `yaml` and
  `py7zr` are the only allowlisted exceptions, each with a parity test.
- **The v1 route surface is frozen.** New surface goes under `/api/v2/`. A minor release
  may add, never change.
- **ADR 0060 ratchets:** surfaces may GROW freely; they may only SHRINK by moving an
  entry to the `retired` ledger, whose reason text is immutable. **Every ratchet lowers
  its baseline in the same commit that earns it.**
- **Every new runtime module must appear in `runtime_modules.txt`** (currently 134
  modules, checked both directions).
- **Test runner: `python scripts\run_windows_tests.py`** — per-file subprocess, 3
  retries. **Never `unittest discover`.**
- `tests/test_frontend_contract.py` must be run as `python -B tests\test_frontend_contract.py`.
- **Do not edit source while `run_windows_tests.py` is running.**
- No `make`, no bash. Use `python -B <script>`. Standalone scripts need
  `$env:PYTHONPATH` and `$env:PYTHONIOENCODING="utf-8"`.
- **PowerShell mangles inline Python.** `[^\"]` and `$(...)` inside a `-c "…"` string
  will be interpreted. Write a `.py` file and run it. (The `echo ""` in a compound
  command also throws `Cannot process command because of one or more missing mandatory
  parameters` — harmless noise, use `Write-Output ""` or drop the separator.)
- **Scratch files to delete before the final gate: `tools_dom.py`, `tools_scan.py`** (both
  untracked, both at the repo root). Use `Move-Item -LiteralPath` if you need to relocate
  them; do not compose `Remove-Item` with `Move-Item`.
- Commit only when the maintainer asks. Branch is `release/1.16.0`; nothing is committed.

---

## 7. Final gate checklist

```
python scripts\run_windows_tests.py      # per-file runner, must be clean
python scripts\check_tests.py            # includes check_dependencies, check_i18n,
                                         #   check_tokens, check_frontend, gen_api_docs --check
python scripts\check_frontend.py
python -B tests\test_frontend_contract.py
node --check static\*.js                # every file
powershell -File scripts\ui_smoke.ps1    # real Chrome; PASSED at last run
```

`ui_smoke` is a real CI job (`.github/workflows/ci.yml:326`) — it is not optional.
Last known good: `motion toast {kept:true, topLayer:true, stacked:true, survived:true,
count:4}`, `toastAboveDialog {raised:true}`, `listPosition {checked:48, setsize:20000,
visibleCount:20000, ok:true}`, `themePaint {changed:true, switched:true, themeCount:6}`,
`cls {toast:0, dialog:0, searchEmpty:0.0546}`.

**Do not assert CLS = 0.** `searchEmpty` measures 0.0546 in steady state — the filter bar
reflows when a search returns nothing. That is recorded as **S55** in the plan and
deliberately deferred to 1.17 as cosmetic. The honest gate asserts the web-vitals
"good" threshold (0.1) **plus** a per-phase zero for the surfaces this release changed.
Asserting literally 0 would be a false claim in the other direction, which is the exact
failure mode this release exists to remove.

---

## 8. Verified state (commands and results, at handoff time)

```
git rev-parse --abbrev-ref HEAD   → release/1.16.0
git status --porcelain            → 83 entries (66 M, 17 ??)
locales/*.json                    → all 5 parse, 0 NUL bytes, 1240 keys each
python scripts\run_windows_tests.py
    → 166/174 passed, 8 files failed
      test_changelog_features      FAIL  (root cause A)
      test_feature_contracts       FAIL  (root cause C)
      test_frontend_contract       FAIL  2 gates (root cause B)
      test_i18n                    FAIL  (root cause B)
      test_launch_phases           FAIL  2 errors (root cause A)
      test_recap                   FAIL  2 errors (root cause A)
      test_session_persistence     FAIL  (root cause A)
      test_sessions                FAIL  (root cause A)
      details: %LOCALAPPDATA%\Temp\openbox-windows-test-results.json
python -B tests\test_frontend_contract.py
    → 71 PASS, 2 FAIL
       FAIL test_counted_strings_have_a_singular_form
       FAIL test_the_singular_exemption_list_is_still_justified
python -B scripts\check_i18n.py
    → FAIL: 1 error(s)
      25 keys referenced in code but missing from en.json
      coverage 100% (1237/1237) in all 5 locales
```

**Everything else passes** — all 17 new test files, the security/integrity/concurrency
suites, the 42 pre-existing defs tests, the 84 pre-existing job-manager tests, and
`test_state_snapshots.py`. The three root causes in §0 are the entire remaining
red. Fix A, B, and C and the suite is clean.

**Modified (66):** `.github/workflows/ci.yml`, `Makefile`, `cloud_sync.py`,
`docs/README.md`, `docs/RELEASE_NOTES.md`, `docs/SUPPORT.md`,
`docs/adr/0004-theme-contract.md`, `docs/adr/0017-controller-settings-ui.md`,
`docs/adr/0018-bios-sha1-drift-detection.md`, `docs/development/PERF.md`,
`docs/reliability.md`, `index.html`, `job_manager.py`, `openbox.py`,
`pkg/parity/parity_backup.py`, `pkg/parity/parity_emulator_defs.py`,
`pkg/parity/parity_emulator_defs_update.py`, `pkg/parity/parity_import.py`,
`pkg/parity/parity_integrations.py`, `pkg/parity/parity_launch_doctor.py`,
`pkg/parity/parity_setup_preview.py`, `pkg/platform_compat.py`, `pkg/state/cache.py`,
`pkg/state/imports.py`, `pkg/state/launch.py`, `pkg/state/sqlite_readmodel.py`,
`plugins.py`, `retroachievements.py`, `scripts/check_i18n.py`, `scripts/check_tests.py`,
`scripts/check_tokens.py`, `scripts/perf_bench.py`, `scripts/ui_smoke.cjs`,
`state_store.py`, `updates.py`, `static/app.css`, `static/arcaderoom.js`,
`static/bigbox.js`, `static/dialogs.js`, `static/gamepad.js`, `static/health.js`,
`static/household.js`, `static/i18n.js`, `static/imports.js`, `static/library.js`,
`static/palette.js`, `static/sessions.js`, `static/settings.js`, `static/setup.js`,
`static/state.js`, `static/util.js`, `static/whatsnew.js`, `static/wrapped.js`,
`tests/test_arcaderoom_contract.py`, `tests/test_ci_gates.py`,
`tests/test_frontend_contract.py`, `tests/test_i18n.py`, `tests/test_launch_doctor.py`,
`tests/test_parity_features.py`, `tests/test_perf_writes.py`,
`tests/test_state_imports.py`, `tests/test_state_snapshots.py`, `tests/test_state_v4.py`.

**New (17):** `scripts/check_dependencies.py`, `scripts/ui_smoke.ps1`,
`tests/test_cloud_sync_concurrency.py`, `tests/test_defs_install_atomicity.py`,
`tests/test_dependencies_gate.py`, `tests/test_emulator_defs_fallback.py`,
`tests/test_import_playlist_safety.py`, `tests/test_job_replace_cancel.py`,
`tests/test_noop_state_writes.py`, `tests/test_path_traversal.py`,
`tests/test_platform_category_cache.py`, `tests/test_playtime_attribution.py`,
`tests/test_plugin_sandbox.py`, `tests/test_readmodel_concurrency.py`,
`tests/test_registry_concurrency.py`, `tests/test_restore_preview.py`,
`tests/test_unchecked_external_values.py`, `tests/test_windows_update_rollback.py`.

**Scratch, must not ship (2):** `tools_dom.py`, `tools_scan.py`.

**Baselines to re-measure if you move a surface:** 134 runtime modules · 24 emulator
definitions (≤24 distinct `app_id`s) · 6 themes · 5 locales · 29 dialogs ·
158 test files · `check_tokens.py`: hex 0, duration 0, easing 0, **z-index 24** (was 25,
ratcheted when the toast container became a popover), radius 68, shadow 28,
showModal 35 · Launch Doctor `preflight_batch` cap `len(items) > 200` → `BadRequest`.

---

## 9. Decisions already made — do not relitigate

- **Single 1.16.0 release**, no `1.15.1`/`1.16.0` split. A patch number cannot carry
  added surface, and one release means one changelog for the security and trust fixes.
- **Phase order is the risk control, not the version number.** Security → data integrity
  → concurrency → false claims → F2 → panels → frontend → trust bugs → F1. §7's cut
  order is binding. Never cut: S1, S2, S3, S4, S7, S8, S10, S11, S12–S17, B1, B2, B3,
  ADR 0064, F2's preview gate.
- **Delete `orjson` rather than declare it.** "Dependency-free" is load-bearing, and a
  *silently divergent* serializer is worse than a declared one.
- **`yaml` and `py7zr` stay, allowlisted**, each with a parity test.
- S1b calibrated to **MAJOR, not BLOCKER**: the defect is confirmed and the fix is three
  `escapeHtml()` calls, but CSP `script-src 'self'` (no `'unsafe-inline'`) blocks inline
  handlers today, so it is one CSP relaxation from full script execution. Do not rely on
  CSP as the sanitizer.
- B1's delegated `cancel` listener uses the **bubble phase** and honours
  `defaultPrevented` — a capture-phase listener would close *behind* the unsaved-changes
  guard.
- The toast container is the popover (per-toast popovers leave the top layer), and toasts
  stack by explicit `bottom` offsets.
- Snapshot ordering is **intrinsic to the filename** — hard links share `st_mtime` with
  the live file, so mtime can never order them.
- A superseded SQLite rebuild **does nothing** — it stops a slow older rebuild clobbering
  newer state.
- S26 `exit_code` is now a plain **int** over SSE, not a namedtuple array; the client
  accepts both shapes so a mixed-version server upgrade is not a regression.
- S51's `would_change` requires **all three** of
  `settings_changed AND settings_restored AND settings_present` — a library-only backup
  carries a settings block in `library.json` that can differ, but `restore_backup()` does
  not write it.
- **ADR 0064 rule 3:** any `ui_smoke` case that exercises the list must also assert the
  list is non-empty, or it is vacuous and proves nothing.
