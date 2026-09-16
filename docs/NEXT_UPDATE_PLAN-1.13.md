# OpenBox 1.13 — Megaplan: "Solid Ground"

Status: active plan (started 2026-09-16 on branch `dev`).
Scope: the largest update since 1.9 — finish the 1.12 deferred wave, fix
data-loss and correctness bugs real users hit, make 20k+ libraries fast,
open the plugin API, and close the test/gate holes that let regressions
through.

Theme: **finish it, fix it, make it fast.** 1.11 and 1.12 added a huge
feature wave (Household, Time Machine, Moments, Radio, kiosk, sync,
collections, story). 1.13 makes that wave *trustworthy* and *instant*,
then lands the deferred headline features on top of a stable base.

> Evidence note: `file:line` references are as of `master @ dec820c`
> (1.12.1). They are signposts, not contracts — re-verify each item against
> the branch before scheduling. Items marked **[verified]** were confirmed
> by reading the code during planning; the rest come from static audits and
> need a live repro before the fix lands.

---

## 0. Release shape

- Version: `1.13.0`, SemVer. v1 route surface stays frozen
  (`scripts/check_v1_contract.py`); all new surface under `/api/v2/`.
- No new runtime dependencies (stdlib + existing frontend only).
- Every new runtime module → `runtime_modules.txt` + `tests/test_<module>.py`.
- Every new visual value → `:root` token in `static/app.css` + all stock
  themes (`scripts/check_tokens.py` ratcheted to 0).
- Coverage floors only go up (`COVERAGE_FLOOR`, `WEB_APP_FLOOR`).
- ADR required for: any new route family, state-boundary change, new sync
  transport, new gate, plugin API freeze, and the state-concurrency model
  change (P1-1).
- Branch note: `origin/dev` is stale (superseded by 1.11/1.12; unique
  commits already re-landed on master). `dev` was recreated from
  `master @ dec820c`; do not merge or rebase the old branch. If the remote
  must keep the name, force-with-lease it or archive it as `dev-1.10`.
- Add the missing `## [Unreleased]` section to `docs/CHANGELOG.md` day one
  so 1.13 work has a landing zone (`scripts/release.sh:86` already expects it).

---

## 1. P0 — Data-loss and blocking bugs (fix before any feature)

These are the "users will find this and lose trust" items. Each gets a
regression test in the same PR.

1. **P0-1 Reload or any tab close silently stops/kills running games.**
   **[verified]** `static/app.js:361` calls `gracefulShutdown()` in
   `beforeunload` whenever `runningGames.length`; the server then
   stop/kills every session (`handlers/sessions.py:117-128`). F5 during a
   game is a user-facing data-loss event (unsaved progress). Fix: do not
   auto-shutdown on `beforeunload`; keep sessions alive across page
   unloads (they are server-side processes), and only stop on explicit
   "Quit OpenBox" / `forceShutdown`. Add a "games keep running when the
   window closes" note to the running-sessions UI.

2. **P0-2 Escape closes the game editor without the unsaved-changes
   guard.** **[verified]** The global Escape handler
   (`static/dialogs.js:41-47`) calls `closeDialog()` directly, which calls
   `dialog.close()`; `preventDefault()` suppresses the native `cancel`
   event, so the guard at `static/dialogs.js:358-364` never fires. Fix:
   route Escape through the dialog's cancel path (dispatch `cancel` or ask
   `openDialog`-registered close guards), or make `closeDialog()` run
   registered guards.

3. **P0-3 A corrupt `library.json` makes the server unable to start, and
   the recovery route is unreachable.** **[verified]**
   `web_app.py:659` runs `update_state(bootstrap_state)` before the HTTP
   server exists; `state_store.py:608-620` raises `StateCorruptError` and
   the process dies. `POST /api/state/recover` exists
   (`handlers/sessions.py:67-114`) but no frontend calls it, and
   `docs/reliability.md` row 4 promises a recovery dialog. Fix: detect
   corruption in `main()`, start a minimal recovery server (or CLI
   `--recover`), surface a dialog that offers last-known-good restore via
   the existing `.bak`/snapshot path, and gate it with a test that boots
   from a deliberately corrupted state file.

4. **P0-4 LaunchBox / ES-DE import apply always 400s for real libraries.**
   **[verified]** `static/imports.js:209-228` posts the entire preview
   report back as `plan`, including `normalized_games`
   (`pkg/parity/parity_launchbox_import.py:496`); `MAX_BODY = 65536`
   (`web_app.py:139,385`) rejects anything past ~100 entries. Any real
   migration fails at the last step. Fix: server stages the preview and
   returns an opaque `preview_id`; apply posts only the id + token
   (keep the digest validation). Add an end-to-end test with >1,000 rows.

5. **P0-5 Bulk edits fail past ~10k selected games.** `POST
   /api/games/bulk(-wizard)` takes an unbounded `ids` array
   (`handlers/library.py:544-550`) while `MAX_BODY` caps the request;
   shift-range select (`static/library.js:953`) can select everything.
   Fix: chunk client-side and batch server-side, or accept a
   "select all matching query" token. Test at 20k ids.

6. **P0-6 Every error toast renders with the *info* style.**
   **[verified]** `notify(levelOrMessage)` defaults to `info`
   (`static/state.js:151-172`); 291 of 301 call sites pass
   `notify(error.message)` with no level. The red banner
   (`opts.actionable`) is unreachable because no caller sets it. Fix:
   detect Error/`error.message` shape and default to `error`, or sweep
   call sites; wire the banner for API failures with code/request id.

7. **P0-7 The changed-line and new-module coverage gates no-op on PRs.**
   `scripts/check_tests.py:42-49` falls back to `HEAD` when there is no
   upstream, and `actions/checkout` gives a detached HEAD on
   `pull_request`; `_check_new_module_coverage` (`:63-84`) depends on the
   same base. The flagship 95% changed-line ratchet is therefore
   decorative on the path code actually enters. Fix: resolve the base from
   the event (`github.event.pull_request.base.sha` / `GITHUB_BASE_REF`),
   fail loudly when the base cannot be resolved, and add a self-test.

8. **P0-8 The locale selector is populated before settings exist, so
   non-English locales are unreachable from the UI.** **[verified]**
   `static/app.js:34-53` reads `AppState.appSettings` at
   `DOMContentLoaded` (still `{}`), falls back to `[{code:'en'}]`, and
   nothing repopulates after `refresh()`. Fix: render the selector after
   settings load, keep localStorage/server locale, and cover with a
   frontend test.

9. **P0-9 In-place mutation of the shared cached state during
   transactions races lock-free readers.** Transactions run the mutator on
   `_cached_state` with `reuse_cache=True` (`state_store.py:883-902`),
   while `load_state_view()` deep-copies the same object outside the lock
   (`pkg/state/cache.py:1004-1017`). Concurrent edit + GET can raise
   `RuntimeError: dictionary changed size during iteration` (surfaced as a
   misleading 400) or observe a half-applied edit. Fix: copy-on-write
   transaction model or reader lock; this needs an ADR and a stress test
   (writer loop + reader loop, 20k games).

10. **P0-10 A read can trigger a state rewrite and fail on a full disk.**
    `_ensure_loaded`/`load` rewrite `library.json` when normalization
    changes state (`state_store.py:578-585`), so a GET can fail with a
    write error. Fix: persist normalization on next real write, or out of
    band; reads must stay read-only.

---

## 2. P1 — Correctness & concurrency (the quiet bugs)

| ID | Area | Evidence | Fix |
|---|---|---|---|
| P1-1 | Error/destructive UX | `confirmAction` ignores its `destructive` flag and always focuses OK (`static/dialogs.js:154-191`) | Destructive style + focus Cancel for destructive actions; test Enter does not delete |
| P1-2 | SSE subscriber cap ignored | `register_event_subscriber` returns False at 16, caller never checks (`web_app.py:500`, `pkg/state/sse.py:232-237`) | Check the result, respond 503 before headers, client retries with backoff |
| P1-3 | SSE never reconnects | `static/sessions.js:99` closes on first error; no backoff (activity.js reconnects) | Shared EventSource manager with reconnect + close on `beforeunload` |
| P1-4 | Duplicate SSE streams | `static/sessions.js:73` and `static/activity.js:369` each open `/api/events` | One shared stream, subscriber fan-out in JS |
| P1-5 | Chunked-encoding body desync | `web_app.py:377-390` reads only `Content-Length`; chunked bytes parse as the next request on keep-alive | Reject/ignore `Transfer-Encoding` explicitly; add a smuggling test |
| P1-6 | Backup not fsynced before replace | `state_store.py:717-725` (`copy2` + `os.replace`, no fsync) | fsync backup file + directory, like the primary path |
| P1-7 | Read paths take the exclusive file lock | `state_store.py:581,647,660` use `LOCK_EX` | Use `LOCK_SH` for reads; measure contention |
| P1-8 | Projection cache keyed by `id(game)` | `pkg/state/cache.py:589-610`; recycled addresses can serve stale projections | Key by stable game id + tracked-field hash |
| P1-9 | Concurrent archive extraction deletes the winner's tree | `archives.py:306-308` (both extract, each rmtree + replace) | Per-digest extraction lock; promote only if absent |
| P1-10 | Archive extraction cache is unbounded | `archives.py:91-94,270-315`; old digest trees never pruned | LRU/budget prune in a background tick + `docs/reliability.md` row 26 honesty |
| P1-11 | `operations_items/*.json` never pruned; unlocked RMW | `pkg/state/operations.py:122-124,279-297,674-696` | Prune with operation records; lock item appends |
| P1-12 | Job name map leaks for process lifetime | `job_manager.py:461,472` | Drop names when jobs are reaped; bound the map |
| P1-13 | Save paths accept any existing path | `handlers/data.py:214-223` (`/etc`, `$HOME` registrable as save root) | Contain to home + approved roots; reject with a clear error |
| P1-14 | Settings creates directories before validation | `handlers/settings.py:292-308` (relative path creates junk then raises) | Validate absolute/existing first, then mkdir |
| P1-15 | `416` returned for non-Range failures | `handlers/media.py:139-144` (any ValueError → 416) | Map only malformed ranges to 416; 404/500 otherwise |
| P1-16 | `/api/health` can 400 on null media fields; stats every path per request | `handlers/library.py:644-697`, `:678,682` | None-guards + cached stats with a TTL |
| P1-17 | Backup list can 500 on a rotating file | `handlers/health.py:75` `item.stat()` inside sort key | Skip unreadable entries |
| P1-18 | Registry lookup failures swallowed into 404 | `routes.py:491-507` | Log + fail loudly; add registry self-test |
| P1-19 | Broad exceptions mapped to 400 | `web_app.py:396-400,531-535` (`KeyError`, `AttributeError`, `RuntimeError` → client error) | Only `BadRequest`/`ValueError` → 400; everything else 500 with request id |
| P1-20 | Theme `@import` guard misses protocol-relative URLs | `handlers/extensions.py:200-202` (`//evil` has no `http`) | Tokenize/parse or reject all `@import` not in an allowlist |
| P1-21 | Export download reads whole file to RAM | `handlers/export.py:37` | Stream from disk with `sendfile`/chunks |
| P1-22 | Launch token validation swallowed | `handlers/launch.py:23-30,47-53` (`except Exception: pass` + discarded result) | Surface invalid tokens; remove the no-op coverage theater |
| P1-23 | `parity_import.BIOS_HINTS` frozen at import contrary to its docstring | `pkg/parity/parity_import.py:41-64` | Lazy `Path.home()` lookup, or fix the docstring + add an env-aware reset |
| P1-24 | Emulator def registry frozen at import; malformed YAML silently dropped | `pkg/parity/parity_emulator_defs.py:232-242,500-501`; blockers for the update channel | Invalidate `_REGISTRY_CACHE` on def change; report malformed defs in the UI |
| P1-25 | `wrapped.js` interpolates server error into `innerHTML` | `static/wrapped.js:33` | Escape like `mastery.js:47` |
| P1-26 | Detail-pane layout inline grid overrides the responsive media query | `static/library.js:288-292` writes `190px minmax(520px,1fr)`, `app.css:775-778` wants a different layout at ≤1100px | Compute from the same breakpoint or drop inline override |
| P1-27 | Stale async responses overwrite the detail pane | `static/library.js:1243-1281` (`renderPlatformDetails` has no selection guard) | Add the `selectedId` stale check used by `loadRelated` |
| P1-28 | Constellation failure leaves "Loading…" forever | `static/constellation.js:167` | Hide spinner, show error + retry |
| P1-29 | Media bulk watcher polls forever after dialog close | `static/media.js:40-48` | Cancel on dialog close / job end |
| P1-30 | Big Box video snap keeps playing in hidden overlay; attract mode covers Party/pause | `static/bigbox.js:68-78,160-167,388-391` | `clearVideoSnap()` on close; exclude all overlays from attract mode |
| P1-31 | Silent persistence failures (sort, grouping, view, profiles, EmuMovies save) | `static/library.js:1472-1473,1537,1565`; `static/state.js:228`; `static/settings.js:910` | Surface failures; show "saved locally / not saved" state |
| P1-32 | Missing double-submit guards | `static/settings.js:1068-1092`, `static/app.js:214-241`, `static/picker.js:23-26`, `static/library.js:1560-1566` | `setButtonBusy` pattern everywhere |
| P1-33 | `promptInput` called with positional args in 5 places | `static/settings.js:566,589,610,612,636` (expects `{title,label,defaultValue}`) | Fix call sites; add a dev-mode arg-shape assert |
| P1-34 | Search worker not nulled on error; dead worker retried | `static/library.js:104-108` | Null on error, recreate on next search |
| P1-35 | Insights never retries after one failure | `static/insights.js:264-267` (`loaded = true` before success) | Set on success only |
| P1-36 | Clipboard copy can't catch async rejection | `static/state.js:202-208` (`writeText` not awaited) | Await + fallback + honest toast |
| P1-37 | Session poll toast spam; "Running (n)" text clobbered by i18n | `static/sessions.js:132-137`, `:128-130`; `static/library.js:879-883` | Single error state, no repeated toasts; compose localized text at render time |
| P1-38 | Unguarded `localStorage` in hot render paths | `static/library.js:253-254,306,324-332`; `static/insights.js:31` | Small `safeStorage` helper |
| P1-39 | Resize handler re-renders the virtual grid every event | `static/library.js:1558` | rAF debounce |
| P1-40 | Dialog opens via raw `showModal()` skip `openDialog()` focus/stack logic | `static/settings.js:351`, `static/media.js:8`, `static/sessions.js:150,178` | Route all opens through `openDialog()` |
| P1-41 | Raw ISO date string-patching everywhere | `static/library.js:1027,1406`, `static/sessions.js:144,185`, `static/recap.js:55`, `static/settings.js:266,286,555`, `static/bigbox.js:268` | Shared `formatDate()` with `Intl.DateTimeFormat`; TZ regression test |
| P1-42 | "Remove game" always chains two destructive dialogs | `static/library.js:1288-1315` | Single dialog with an explicit "also delete media" checkbox |
| P1-43 | Facet values render raw English field names | `static/state.js:415-417,435-436` | Localize field labels |

---

## 3. P2 — Performance & scale (target: 20k games feel instant)

1.12 made the state cache durable but every request still deep-copies the
whole library. Measured during planning: ~133 ms per 20k deepcopy, twice on
a cache miss. New budget: **p95 `/api/library` at 20k games ≤ 250 ms warm,
≤ 800 ms cold; any single write ≤ 50 ms above baseline.**

| ID | Hotspot | Evidence | Direction |
|---|---|---|---|
| P2-1 | `load_state_view()` deep-copies per request (twice on miss) | `pkg/state/cache.py:1004-1017`; 62 call sites | Immutable snapshot + refcount, or structural sharing; never copy for read-only JSON serialization |
| P2-2 | `update_state()` deep-copies after every mutation | `state_store.py:809-812`; `openbox.py:138-145` | Return only what callers need; stop copying on the hot path |
| P2-3 | Every write projects the full catalog twice for sync journaling | `openbox.py:107-122`, `pkg/parity/parity_library_sync.py:518-535` (~76 ms/snapshot @20k) | Diff only touched ids; batch journal writes |
| P2-4 | Auto-import loop wakes every 10 s, rescans, rewrites even when nothing changed | `pkg/state/imports.py:282-341,189-206` | Cheap mtime/size fingerprint first; write only on change; back off |
| P2-5 | Save discovery walks RetroArch trees per game on a 2 s cache | `pkg/state/cache.py:886`, `pkg/parity/parity_saves.py:69-90`, `saves.py:171-176` | Walk once, build an index by stem; cache longer |
| P2-6 | Bulk media download does O(n²) state load/write | `handlers/media.py:283-318` | One transaction for the whole batch, per-item results |
| P2-7 | Media probe cache thrashes at >20k games | `handlers/media.py:63-89`, `pkg/state/media_probe.py:92-96` | Larger/segmented cache; negative caching; batch stats |
| P2-8 | SQLite "FTS" search is a full-table JSON scan; facets never wired | `pkg/state/sqlite_readmodel.py:296-324`; `handlers/library.py:367-375` calls JSON facets | Real FTS5/LIKE at the SQL layer; port facets; call `query_parity_check` |
| P2-9 | `/api/notifications` list performs a full state write | `handlers/library.py:480-491` | No-op mutator must not transact |
| P2-10 | Flatpak status spawns ~15 subprocesses; `install_all` is O(n²) | `emulators.py:18-39,65-77,93-104` | Cache status with TTL; single status call per loop |
| P2-11 | Emulator install runs `flatpak install` synchronously in-request (1800 s) | `handlers/imports.py:285-295` | Job + progress + cancel |
| P2-12 | ludusavi/hoard save tools block the request thread up to 600 s | `handlers/data.py:320-337` | Job wrapper like library backup |
| P2-13 | Folder import scans in the request thread | `pkg/state/imports.py:270-307` | Job + progress, streaming preview |
| P2-14 | Operation progress deep-copies the full checkpoint per item | `pkg/state/operations.py:514-518`; `handlers/media.py:316-317` | Store deltas/append-only ids |
| P2-15 | Unbounded connection threads; per-read timeout only | `web_app.py:686,140-149` | Connection cap + total request deadline |
| P2-16 | Search facets count on the UI thread | `static/worker.search.js:141` | Move facet counting into the worker |
| P2-17 | Cold-start profile missing for 50k | `docs/development/PERF.md:5-9` still has no 1.12 baseline | Fresh baseline + 50k tier; ADR 0042 warm-up/trimmed-p95 rules |

Also: perf gate must cover the new SQLite path and the write path
(P2-2/P2-3), since those are where regressions will hide.

---

## 4. P3 — Security hardening

- **P3-1** Updater error paths untested: `updates.py:216-259`
  (`load_release_signature`, `verify_update_signature`, `_release_public_key`
  failure branches) at ~75% coverage. Add malformed-signature, bad-key,
  truncated-payload, and rollback tests. Security-critical: no happy-path-only.
- **P3-2** Release AppImage workflow installs `cryptography==50.0.0` while
  `requirements-dev.txt` pins `50.0.1` — two signer builds. Pin one.
- **P3-3** Zip/archive extraction: add an explicit zip-slip/tar-slip and
  absolute-path test for `archives.py` + `saves.py` restore.
- **P3-4** Add a small local-security self-check to `make check`: token
  required on mutating routes, loopback binding, CSP on document responses
  (extend `scripts/check_csp.py`), no `shell=True` with interpolated input.
- **P3-5** Re-confirm AppImage signature verification cannot be skipped via
  env var or legacy `.zsync` path; add an ADR note if it can.
- **P3-6** Plugin runner sandbox: `plugin_runner.py` at 0% test coverage
  (`runtime_modules.txt:17`). Prove no writes outside the declared dir, a
  timeout on plugin execution, and a test for both.
- **P3-7** `docs/SECURITY.md` threat model update: local token model, plugin
  trust, sync-folder trust, import file trust.

---

## 5. P4 — Headline features (the deferred 1.12 wave)

Ship 4 as the face of 1.13, land the rest as stretch. All new routes under
`/api/v2/`, all state changes behind existing boundaries.

1. **F1 — Now Playing presence (Household 2.0).**
   No `parity_presence.py`, no heartbeat, no activity feed today
   (`static/household.js:78`). Heartbeat with ~10 min TTL written as a
   signed event into the sync folder; `GET /api/v2/household/activity`
   projects "who's playing what"; live strip + optional rate-limited toast;
   off by default, per-member opt-in, no retained history. Reuse
   `parity_household` signing + `launch_tokens`. ADR for the transport.

2. **F2 — Deck Builder for Game Night.**
   `parity_party` can build a queue but cannot save/load/share one
   (`pkg/parity/parity_party.py:63,83,151`; `static/party.js` has no
   save/preset code). Named queues, theme presets ("90s racers",
   "co-op only", "8+ players"), deterministic seeded shuffle so a shared
   queue reproduces identical order from the seed alone.

3. **F3 — Time Machine compare & bounded revert.**
   `handlers/timemachine.py:56,74,87` has events/as-of/revert but no
   compare; `static/timemachine.js` has no diff UI. "Compare two dates":
   added/removed/re-edited between two points; whitelist revert apply for
   metadata fields only (never paths or launch config). ADR for the
   whitelist.

4. **F4 — Save History.**
   `handlers/data.py:27` returns name+size; `static/sessions.js:214`
   renders name-only buttons. Per-game save versions with age, size,
   source (manual/auto/pre-launch), restore, and a verify step that reads
   the archive back. Reuse the backup diff/preview pattern (ADR 0019).
   Optional opt-in save sync over the causal transport (ADR 0039) as
   stretch.

5. **F5 — Artwork Doctor.**
   Missing covers / low-res / wrong-aspect / duplicate-art report with
   one "fix all with SteamGridDB" batch job (provider exists;
   `handlers/steamgrid.py:207-217` only fills empty covers). Per-item
   progress, cancel, undo, provider attribution. New module
   `pkg/parity/parity_artwork_hygiene.py`.

6. **F6 — Per-platform setup checklists.**
   Replace "why doesn't PS2 work" with a green card: BIOS present
   (SHA1 drift, ADR 0018), emulator resolved, one game launches, artwork
   fetched. Data already in `parity_emulator_defs` + BIOS hints + launch
   doctor; this is a projection + UI.

7. **F7 — Plugin API v1 + real catalog.**
   Freeze what `plugins.py` exposes (library read, palette commands,
   detail-pane tabs, notifications) with SemVer + `docs/plugin-api.md`;
   add `register_command()` so plugins reach `static/palette.js` (ACTIONS
   is static today, `static/palette.js:9-18`); publish real catalog
   entries (`plugins/catalog.json` has a single `local_only` stub and
   `plugin_catalog.py:15` pins a commit hash) with signed, versioned
   downloads. ADR for the API freeze.

8. **F8 — High-contrast stock theme.** Sixth theme proves the token
   contract and serves real users; theme test suite gains a contrast-ratio
   check (WCAG AA for text tokens).

9. **F9 — First-run "try these" cards.** After setup: pick a game, run a
   Radio query, open Arcade Room, connect a save folder. Currently setup
   just posts `welcome_completed` (`static/setup.js:905`).

10. **F10 — Auto-Moment wiring.** `auto_moment_trigger()`
    (`handlers/moments.py:312`) is test-only. On session end with a trophy
    or progress advance, offer "Capture this moment?" (one click bookmarks
    the recap timestamp).

11. **F11 — Kiosk PIN backoff.** `handlers/arcade.py:111-115` accepts
    unlimited attempts. Exponential in-memory backoff + honest docs (PIN is
    a convenience boundary, not a security boundary).

12. **F12 — Constellation: viewpoints, path, PNG.** Save pan/zoom/filter
    viewpoints, BFS "path between two games", export via `toDataURL`
    (`static/constellation.js` has none today).

13. **F13 — Emulator def update channel.** Signed, versioned YAML defs
    fetched like `plugin_catalog` with `openbox-release.pub`; unblocks
    P1-24. ADR for the trust model.

14. **F14 — Background update download + apply on restart** (AppImage,
    reusing `.zsync`), with a Settings toggle and honest progress.

15. **F15 — Household weekly auto-challenge + shelf share.** Week-seeded
    deterministic challenge all members agree on; publish a filtered game
    list (not files) as wishlist entries.

---

## 6. P5 — Missing features users ask for (discovery list)

Verify each against current behavior; these are the gaps audits surfaced
that aren't in any existing plan.

- **Missing-file repair wizard.** Health detects missing paths; add a
  relink/bulk-repoint flow ("find in folder…") that fixes records, not
  just reports them.
- **Duplicate detection & merge.** Same title/platform/path collisions
  with a merge tool (keep the record with sessions/trophies, union the
  media).
- **Portable mode / data-dir picker.** `OPENBOX_DATA_DIR` is env-only;
  surface it in Settings → Data with a restart prompt.
- **Undo for destructive actions.** Trash exists; add a timed "Undo"
  toast for trash/purge/bulk edits where feasible.
- **Bulk metadata refresh with cancel/ETA** and per-game results, reusing
  the job panel (jobs exist; UX doesn't).
- **Keyboard shortcut cheat sheet** (`?` dialog) generated from the real
  handler map, not a static list.
- **Save backup "test restore"** drill: restore to a temp dir and verify
  file count/checksums before trusting a backup.
- **Controller-native parity.** `native_host.c:1541` `onGamepad` is a
  no-op stub (`handlers/native.py:30`); couch users rely on keyboard.
  Either implement or remove from the contract and document.
- **Session notes / journal export** (markdown/CSV) per game and per
  household member.
- **Artwork field picker** per slot (cover/hero/logo/icon) with provider
  attribution and history.
- **Collection import/export** with library export (collections are
  query-based; make them portable).
- **Importers:** Heroic, Lutris, itch.io, GOG Galaxy, Epic (PARITY has
  EA/Ubisoft/Xbox as partial). Needs a legal/reliability note per source.

---

## 7. P6 — UX polish (from the audit)

- Error taxonomy in the UI: distinguish info/success/warning/error
  (follows P0-6); API errors carry `code` + `request_id` in the banner.
- Loading/empty states everywhere (constellation, insights, timelime,
  household) with a retry affordance.
- Consistent destructive confirmation (P1-1) and a "don't ask again for
  this action" option (stored per profile).
- Responsive pass 360–1100px: the detail workspace (P1-26), bulk bar,
  activity drawer, Big Box overlays.
- Toast queue instead of a single reused toast element (concurrent
  actions currently clobber each other).
- Command palette: `>` runs Launch Doctor, `?` searches a generated help
  index (backtick `t()` keys and `static/palette.js:131` dead ends are
  already identified); recent-usage ranking is done.
- Settings search should deep-link to the matched control (highlight +
  scroll), not just filter.
- `notify` banner "copy diagnostics" (P1-36) with the request id.

---

## 8. P7 — Accessibility

- Virtualized grid: real roles (`grid`/`row`/`gridcell` or
  `listbox`/`option`), `aria-rowcount`/`aria-rowindex`,
  `aria-setsize`/`aria-posinset`; fix `aria-selected` on buttons without
  roles (`static/library.js:490-494`).
- Every dialog routes through `openDialog()` (P1-40); focus trap audit.
- Timeline entries are clickable divs — make them buttons
  (`static/timeline.js:22-39`), label covers.
- `data-i18n-aria-label` for the 71 hardcoded `aria-label`s in
  `index.html`.
- Reduced-motion audit: `mood.js`, party wheel spin, constellation intro,
  animated covers — extend the `arcaderoom.js`/`bigbox.js` guard.
- Live regions for background job progress (beyond `#activityCount`).
- High-contrast theme (F8) + contrast check in the token gate.

---

## 9. P8 — i18n

- **Re-render on `localechange`.** Only `ensureTrashViewOption`
  (`static/library.js:1440`) listens today; the JS-rendered library,
  sessions, activity, insights, media, dialogs stay in the old language
  until reload. Add a small `onLocaleChange(render)` registry.
- **Wire the shipped-but-unused namespaces** (`achievements.*`,
  `backup.*`, `playlists.*`, `bigbox.*`, `setup.*`, `sessions.*`) — the
  markup never got `data-i18n`; 364 keys unreferenced.
- **`static/setup.js` never imports `t`** (999 lines, English-only).
- Hardcoded English sweeps with counts: settings 93, library 69,
  metadata 45, setup 29, sessions 24, app 22, dialogs 21 sentence-like
  literals; `state.js` badge text; `activity.js` drawer headings.
- Gate: `scripts/check_i18n.py:26` misses backtick template `t()` calls
  (`static/dialogs.js:548,588`, `static/moments.js:163,444`); add them and
  make unused-key count visible but ratcheted downward.
- Locale fetch failure is silently swallowed (`static/i18n.js:28-53`);
  show a warning when falling back to English.
- Community locale docs in `docs/CONTRIBUTING.md`; accept locale #6.

---

## 10. P9 — Tests, gates, release engineering

| ID | Gap | Evidence | Fix |
|---|---|---|---|
| P9-1 | Changed-line/new-module gates no-op on PRs | P0-7 | Event-aware base SHA + loud failure + self-test |
| P9-2 | `make check` ≠ CI (`shellcheck`, packaging, ui smoke, perf, CodeQL are CI-only); CONTRIBUTING claims full gate | `Makefile:29-30`, `.github/workflows/ci.yml` | Add a `make check-ci` (or run all) and fix CONTRIBUTING wording |
| P9-3 | `.venv-dev` recreated + pip install every `make check` | `Makefile:21-23,29` | Conditional venv/install; offline-friendly |
| P9-4 | Tests retried 3× and only final result reported; flaky tests invisible | `scripts/check_tests.py:244-253` | Report retries, fail on flake, fix flakes |
| P9-5 | No per-test timeout; one hang kills the CI job | `scripts/check_tests.py:245`, `run_all_tests.sh:32` | Per-file timeout + attribution |
| P9-6 | Environment skips reported as PASS with no accounting | `run_all_tests.sh:32-38` (gamescope, 7z symlink, webkit headers) | Count and print skips; fail CI on unexpected skips |
| P9-7 | Single-file runs can write to the real library | `Makefile:33-34` vs `run_all_tests.sh:15-17` | `make test-one` must export an isolated `OPENBOX_DATA_DIR` |
| P9-8 | Coverage floors a point below measured; stale pins in `tests/test_ci_gates.py:47-53` | `scripts/check_tests.py:31-34` | Ratchet both places in one commit; derive the pin from the gate |
| P9-9 | `plugin_runner.py` 0% covered; updater error paths 75%; thin suites in tracking 40%, igdb 41%, extensions 48%, wine 52%, premium 52%, clips 53%, backup 72% | coverage + `runtime_modules.txt:17` | Targeted suites, especially plugin runner + updater |
| P9-10 | Gate scripts themselves untested (tokens/csp/version/runtime/frontend); `test_ci_gates.py` asserts strings, not behavior | tests/ | Table-driven tests per check script |
| P9-11 | `runtime_modules.txt` globs miss generic `pkg/*.py`; touched-module floor not enforced in `make check` | `scripts/check_runtime_modules.py:50-57`, `scripts/check_tests.py:87-102` | Extend globs; call `check_changed_coverage` from `make check` |
| P9-12 | Token gate scans only CSS; raw values in `index.html`/JS/SVG pass | `scripts/check_tokens.py:25` | Extend scan or document the limitation honestly |
| P9-13 | No markdown link/index gate despite ADR 0041's complete-index intent | docs | Link checker; docs index test |
| P9-14 | Release workflows don't run the gate or version sync; one unsigned/unattested Flatpak | `.github/workflows/release-*.yml` | Gate release on green `make check` + `check_version_sync.py`; attest Flatpak or document |
| P9-15 | Release version bump is fully manual across updates.py/README/metainfo/CHANGELOG/PARITY | `scripts/release.sh` | `scripts/bump_version.py` + a dry-run check |
| P9-16 | 22 test files depend on suite-wide `OPENBOX_DATA_DIR` env | tests/ | Isolate in-module like the 1.12.1 fix |
| P9-17 | README CI badge is a static "passing" shield | `README.md:26` | Use the workflow status badge or remove |

---

## 11. P10 — Docs & API surface

- **D1** Generate `docs/api-v2.md` from the route registry + freshness gate
  (planned in 1.12, never shipped; `scripts/gen_api_docs.py` is v1-only and
  not gated). Also generate a v1 section so the freeze is visible.
- **D2** `docs/plugin-api.md` + SemVer policy (F7).
- **D3** Fresh PERF baseline for 1.12/1.13 with the SQLite path on
  (`docs/development/PERF.md:5-9` says no 1.12 run was recorded).
- **D4** `docs/reliability.md` honesty pass: row 2 (ENOSPC message with
  data dir + free-space hint) is unimplemented; row 3 lock wording (P1-7);
  row 26 archive cache bound (P1-10). Every row must name its test.
- **D5** `docs/flathub-checklist.md` + screenshots/metainfo URLs stale at
  1.11.0 despite 1.12 releases; refresh to a 1.12.x tag (or drop screenshots).
- **D6** `docs/PARITY.md` partial rows (EA/Ubisoft/Xbox, >20k exploratory,
  English fallback labels, Household partial) reconciled with P5 items.
- **D7** Stale-version sweep automation: extend `check_version_sync.py` to
  pin the lead paragraphs of README/RELEASE_NOTES, not just "v{version}
  appears somewhere" (`scripts/check_version_sync.py:108-113`).

---

## 12. Kill list (delete before adding)

- Root disk junk (untracked but confusing): `OpenBox-x86_64-final*.zsync`
  (×5), `OpenBox-x86_64-1.10.0.AppImage.zsync`, `OpenBox-x86_64.AppImage`,
  `.coverage`; stale `build/flatpak-*` trees. `test_packaging.py:52`
  defaults to the root AppImage, so this also fixes test nondeterminism.
- No-op coverage theater: `handlers/launch.py:27,50-53`,
  `handlers/emulators.py:60-63`.
- Dead `FacetCache` budget/DEGRADED machinery (`pkg/state/cache.py:87-198`)
  — `compute_facets`/`get_facets` have no callers.
- `verifyWorkerParity` (`static/library.js:158,1582`),
  `markSearchIndexDirty`/`_searchIndexDirty` (`static/state.js:266`),
  unused imports (`static/app.js:28`).
- Evaluate `pkg/parity/parity_premium.py` (52% coverage, no product
  surface): ship it or cut it — no speculative abstraction.
- Test-only helpers that nothing runtime calls (`parse_cue`,
  `chips_to_rules`, `best_match`/`match_pair_key`, several
  `parity_household` helpers): either wire them or delete with their tests.
- Plugin-list silent skips (`plugins.py:136-139,85-90`): show malformed /
  unsandboxed plugins in the manager instead of hiding them.
- Docs: `docs/SPACING_MEGAPLAN.local.md` is untracked but sits in the docs
  tree; move to `docs/archive/` or delete.

---

## 13. Suggested milestones

**M0 — Process (day 1)**
- Branch: recreate `dev` from master (done); archive or force-with-lease the
  old `origin/dev`.
- Add `## [Unreleased]` to `docs/CHANGELOG.md`; draft release notes early.
- Fix P0-7 (gate diff base) first so nothing else can merge untested.

**M1 — Data safety (1.13.0 must)**
- P0-1 reload kills games · P0-2 Escape loses edits · P0-3 corrupt-state
  startup + recovery UI · P0-4 import apply >64KB · P0-5 bulk edit cap
- P0-6 error levels · P0-8 locale selector · P0-9 state concurrency ADR +
  fix · P0-10 read-triggered rewrite

**M2 — Correctness sweep (1.13.0 must)**
- P1-1…P1-8, P1-13, P1-16, P1-19, P1-25, P1-26, P1-32, P1-33,
  P1-40, P1-41, P1-42

**M3 — Performance campaign (1.13.0 must)**
- P2-1…P2-4, P2-8, P2-9, P2-15 with the 20k budget and a fresh 50k tier

**M4 — Headline wave A (1.13.0)**
- F1 Presence · F2 Deck Builder · F3 Time Machine Compare · F4 Save History

**M5 — Headline wave B (1.13.0 if capacity, else 1.13.1)**
- F5 Artwork Doctor · F6 Setup Checklists · F7 Plugin API v1 · F8
  High-contrast theme · F9 Onboarding cards

**M6 — Accessibility/i18n/polish (1.13.x)**
- Sections 7–9

**M7 — Remaining features (1.14 candidates)**
- F10–F15, P5 discovery list

**1.13.0 ship gate**
- `make check` green with ratcheted floors (target ≥85% total, ≥62%
  web_app), `docs/api-v2.md` generated, changelog complete, ADRs merged,
  no open P0/P1 items, perf budget measured and recorded in PERF.md.

**Explicitly out of scope (say so in release notes)**
- No cloud hosting, no accounts, no mobile app — local-first is the brand.
- No new storefront scraping without legal review.
- No telemetry of any kind.

---

## 14. ADRs this release needs

- State concurrency model for reads/transactions (copy-on-write vs reader
  locks) — P0-9.
- Import apply transport (staged preview handle) — P0-4.
- Presence heartbeat transport + TTL + privacy defaults — F1.
- Party deck sharing / seeded shuffle contract — F2.
- Time Machine compare output schema + revert whitelist — F3.
- Save history storage, retention, and restore verification — F4.
- Artwork fix-all batch policy + provider attribution — F5.
- Plugin API v1 freeze + catalog trust model — F7.
- High-contrast theme + contrast-ratio gate — F8.
- Emulator def update channel trust — F13.
- Gate diff-base resolution and fail-loud policy — P9-1.
- api-v2 generated-reference gate — D1.

---

## 15. Execution log

- [x] M0 branch/process — `dev` recreated from master; gate diff-base
  resolver + CI wiring + tests (ADR 0048); `[Unreleased]` changelog section.
- [x] M1 data safety — P0-1 reload keeps games running, P0-2 Escape guard,
  P0-3 corrupt-state recovery (boot test + HTTP test), P0-4 import apply
  token-only, P0-5 bulk chunking, P0-6 error toast levels, P0-8 locale
  selector, P0-9 consistent snapshot reads (ADR 0049), P0-10 reads never
  write.
- [x] M2 correctness — all P1 rows landed, including archives/operations
  safety, save-path containment, lazy BIOS hints, hot-reloadable emulator
  defs, shared read locks, content-keyed projection cache, media/health/
  registry correctness, error taxonomy, streaming export, and the toast
  queue. P1-6 (backup fsync) intentionally deferred: the mirror backup and
  the one-fsync-per-commit budget are deliberate; revisit with the write
  path.
- [x] M3 performance — structural sharing (ADR 0054), touched-id
  journaling, idle auto-import, save index, single-transaction bulk media,
  SQLite FTS/LIKE search + facets, probe batch cache, emulator status TTL
  and background installs, worker facets, connection cap/deadline, 50k perf
  tier. Measured wins recorded in `docs/development/PERF.md`.
- [x] M4 headline wave A — presence (ADR 0050), deck builder (ADR 0051),
  Time Machine compare/revert (ADR 0052), save history (ADR 0053).
- [x] M5 headline wave B — artwork hygiene (ADR 0055), setup checklists,
  plugin API v1 (ADR 0056), high-contrast theme (ADR 0057), onboarding
  cards, auto-moment, PIN lockout, constellation extras, signed defs
  channel (ADR 0058), background update download (ADR 0059), household
  weekly challenge + shelf share. Deferred: zsync delta reuse in the update
  path and plugin detail-pane tabs (documented in `docs/plugin-api.md`).
- [x] M6 a11y/i18n/polish — grid/list ARIA, dialog adoption guard,
  reduced-motion rules, activity live region, `onLocaleChange` registry,
  setup.js localization, backtick `t()` gate coverage, locale fallback
  warning, palette Launch Doctor/help index, CONTRIBUTING locale docs.
- [x] M7 remaining features — repair wizard (`parity_repair`), duplicates +
  merge (`parity_duplicates`), save test-restore drill, session journal
  export, undo action toasts, shortcut cheat sheet, collection
  export/import, toast-queue smoke assertions, and all dialog opens through
  `openDialog()`. Backlog (explicitly not shipped in 1.13): portable
  data-dir picker (needs restart transport + ADR), bulk metadata refresh
  cancel/ETA, controller-native parity, extra storefront importers,
  animated cover hover, QR phone remote.
- [x] Gate — `check_tests.py` passes end to end: 179 test files, coverage
  85% (floor 83), web_app 73% (floor 73), new modules 87% (floor 85),
  changed-line pass, tokens 0, docs links, api-v2 freshness.
- [ ] 1.13.0 release — not tagged; all work is uncommitted on local `dev`.

Backlog carried to 1.14 is listed in the M7 entry above and in section 6
(P5 discovery items not marked done).
