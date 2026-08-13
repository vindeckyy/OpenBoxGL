# OpenBox Production Polish Plan (v0.9 -> 1.0)

Owner: OpenBox maintainers
Status: Draft for review
Scope: backend, frontend, reliability, security, performance, packaging, release trust
Explicitly out of scope: new feature surfaces, subscription tiers, cloud-hosted services

This plan turns OpenBox from a well-tested hobby launcher into software a company would ship and stand behind. The goal is not "looks nicer". The goal is: every shipped release is reproducible, measurable, recoverable, and honest about what it can and cannot do.

---

## 0. Ground truth

### 0.1 What is actually in the repo today (verified)

- ~9,500 lines of Python across ~40 runtime modules, single loopback `ThreadingHTTPServer`, JSON-on-disk state (`state_store.py`, schema v4), one 3,117-line `index.html` (2,255 lines JS, 375 CSS, 483 HTML).
- ~140 JSON responses in `web_app.py`; route dispatch is two long if-chains: `_do_GET` (613 lines, ~93 routes) and `_do_POST` (195 lines, ~47 routes).
- 39 self-running unittest files, no pytest, no coverage measurement, no linter in CI. CI: test matrix on Python 3.10 and 3.12 only.
- 21 dialogs in the UI, 156 buttons, 12 global event listeners, 92 distinct API calls from the frontend.
- State already has: stable game IDs, atomic writes with fsync, last-known-good `.bak`, recovery endpoint, secret redaction in logs, loopback token auth, bounded plugin subprocesses, validated webhooks, CSP headers, per-request body cap.
- Design system documented in DESIGN.md and consistent with the live tokens in index.html. Prior external design review exists in `.commandcode/design/` (score 35/60, "watch").
- Docs are split: app docs here, real docs site in a separate repo (`openboxgl.github.io`).
- PLAN.md (the current work plan) is essentially complete: alias table, manual finder, coverage stats, native window polish. This plan assumes those land first or in parallel with Phase 1.

### 0.2 The three real problems this plan fixes

1. **The codebase is structurally amateur even though the behavior is not.** Two monolithic dispatchers, a single 3k-line HTML file, no lint/type/coverage gate, no API contract, ad-hoc error strings. A company cannot ship this because a company cannot safely change this.
2. **The product is not verifiable.** There is no coverage number, no performance gate, no reproducibility story for the AppImage, no documented support matrix, and no way to prove a release did not regress.
3. **The trust surfaces are half-finished.** Update checks and downloads are HTTPS + checksum verified (good), but there is no signature/provenance story, no dependency inventory for the AppImage, no crash reporting, and the docs/release process still relies on manual steps.

### 0.3 Principles for every phase

- **Measure before changing.** Every perf claim gets a number from `scripts/perf_bench.py` before and after.
- **No behavior break without a contract.** Backend changes keep old routes working; the UI moves to new routes file by file.
- **stdlib first.** The project already lives on this rule. New deps need written justification; none of the phases below strictly require a new runtime dependency.
- **Fix structure before pixels.** Phases 1-2 are structural. Phase 3 is pixels.
- **Ship in slices.** Each phase ends with the full test suite green, a versioned commit, and CHANGELOG entries.

---

## Phase 0: engineering foundation (week 1)

Goal: make "does this repo work" answerable by a machine in under two minutes.

### 0.1 CI hardening

- Add `python3 -m py_compile` over `runtime_modules.txt` plus all test files as a CI step.
- Add a coverage run. Options in stdlib-first order: `python3 -m trace` is too slow and noisy for 39 files; adopt `coverage.py` as a dev-only dependency (not shipped in the AppImage, not imported at runtime). Pin it in a new `requirements-dev.txt` or a `dev` extra of a future pyproject.
- Add a lint gate: `ruff check` and `ruff format --check` on a minimal, explicit config (line length 120, no bandit-style deep rules at first; enable the dangerous-constructs rules as a second pass). Phase 1 pays off the existing debt; Phase 0 just turns the gate on and allows the current baseline to pass.
- Add a branch-protection-friendly required status: tests on both Python versions + lint + coverage report with a floor (see 0.3).
- Add `timeout-minutes` to both workflows so a hung test cannot burn CI hours.
- CI test step: keep the bash loop but add `set -euo pipefail` behavior is already there; add per-file timing (`date +%s` around each file) so slow tests are visible.
- Add a weekly schedule trigger for CI on `master` so bit-rot surfaces without a PR.

### 0.2 Coverage floor

- Run coverage once on `run_all_tests.sh` to get the honest baseline. Expect something in the 30-50% range; do not panic, do not set a fake floor.
- Set the CI floor to the measured baseline minus 2 points, and raise it by 1 point in each subsequent phase. The floor must never be allowed to silently drop: CI fails if coverage regresses past the floor.
- Maintain an explicit `# pragma: no cover` policy: only for defensive branches that cannot be reached in tests (e.g., `if TYPE_CHECKING`), and require a comment explaining why.

### 0.3 Test infrastructure

- Keep the `python3 -B test_*.py` convention (it is a feature: zero-install tests on any Linux box). Do not migrate to pytest in Phase 0.
- Add a `scripts/check_tests.py` (or a Makefile target `make check`) that:
  1. Runs the whole suite with `coverage`.
  2. Runs `ruff check`.
  3. Runs `py_compile` over runtime modules.
  4. Prints a single summary block: tests passed, coverage, lint violations, compile errors.
- Add a `make` target `test-one TEST=test_saves.py` for developer ergonomics.
- Enforce in CONTRIBUTING.md: any PR touching `web_app.py` must add or update a test that exercises the route.

### 0.4 Repository hygiene

- Fix the small stuff that makes the repo look unmaintained: `CONTRIBUTING.md` still points at `vindeckyy/OpenBoxGL` (correct today, but the org migration to `OpenBoxGL` should be a tracked issue, not a surprise); `docs/` scaffolding README must stay (it documents the split); `.commandcode/` is gitignored already (verified).
- Add a `CODEOWNERS` file if there is more than one maintainer; add issue templates for bug/feature with the fields that actually help triage (log snippet, AppImage version, distro, `python3 --version`).
- `git ls-files` check: confirm no generated artifacts (screenshots, node_modules, AppImages) are tracked. Currently only `scripts/node_modules` risk exists; it is gitignored. Add a CI step that fails if `git diff --check` finds whitespace errors.

### 0.5 Version and metadata discipline

- Centralize the version: today `VERSION` lives in `updates.py` and must be synced across README badge, metainfo, PARITY.md, and the bug template by hand. Add a CI check (small Python script) that reads `updates.py`'s version and fails if any of the known spots disagrees. This kills the "forgot to bump one of four files" class of release bug forever.
- Add `version` to `/api/health` output (it is the only endpoint already unauthenticated and it already exists).

### 0.6 Phase 0 exit criteria

- [ ] CI: tests (3.10 + 3.12), lint, py_compile, coverage floor, version-sync check, all green.
- [ ] `make check` runs everything locally with one command.
- [ ] Coverage baseline recorded in a `COVERAGE.md` table (date, number, delta).
- [ ] CONTRIBUTING.md updated with the new gates.

---

## Phase 1: backend hardening (weeks 2-4)

Goal: the HTTP layer becomes a real, versioned, testable API instead of two if-chains.

### 1.1 Route registry (no behavior change)

Extract routing without changing any response bytes:

- Add `routes.py`: a table of `(method, path_pattern, handler_name)`. Handler names keep the existing `Handler.<name>` methods untouched.
- `_do_GET` and `_do_POST` become: parse path, match against the table (exact match first, then the existing prefix/query logic preserved), dispatch.
- Serve `index.html` and static assets from the table too.
- Keep the existing behavior for unknown paths (404 with a consistent JSON error) but centralize it: today some unknown paths return a bare 404 and others return JSON. Pick one contract: JSON everywhere for `/api/*`, HTML/plain for non-API paths.
- No test changes required: this is a refactor. This is the one time "no test changes" is the success criterion.

### 1.2 Error model

- Define an exception taxonomy in a new `api_errors.py`: `ApiError(status, code, message, detail)` with subclasses `BadRequest`, `NotFound`, `Conflict`, `ServiceUnavailable`, `Unauthorized`.
- `_handle_request` maps: `ApiError` -> its status + `{error, code, detail}`; `StateCorruptError` -> 503 with the existing recovery message; `ValueError` from handler code -> 400 with the message (today some `ValueError`s reach the generic 500 path, which lies to the user); anything else -> 500 with the existing "copy the diagnostic log" message.
- Every handler gets an error `code` in the JSON so the UI can branch on it: `GAME_NOT_FOUND`, `LAUNCH_FAILED`, `MEDIA_JOB_RUNNING`, `WEBHOOK_DESTINATION_BLOCKED`, etc. Start with the 15 most common ones; do not enumerate all 140 endpoints.
- Add `Request-Id`: generate a short id per request, include it in responses and in log lines, and show it in the UI's error surfaces. This is the single highest-leverage debugging feature for a local app.

### 1.3 API versioning

- Add `/api/v1/*` as the versioned surface. Mechanical rule: v1 handlers call the exact same domain code; the legacy paths stay and keep working.
- Header-based detection: if the client sends `X-OpenBox-API-Version: 1`, respond with the v1 routes' shapes (already the same shapes for now). If it sends anything else, 400 with a list of supported versions.
- Move the UI off hardcoded strings by introducing one `API` map in the frontend (Phase 2), so a future v2 rename is a one-line change.
- The 1.0 promise: v1 routes are frozen at 1.0. Any future change goes to v2 or is additive (new fields never break v1 clients).

### 1.4 Request/response contracts

- Adopt a small `contracts.py` module with plain functions: `require_fields(payload, ...)`, `optional_str`, `bounded_list`. Handlers move from inline `str(payload.get(...))` casts to these helpers over time; do the 20 highest-traffic handlers first (`save_game`, `save_settings`, `launch`, `queue`, `bulk`, media endpoints).
- Response compression: gzip bodies over 1KB for JSON (and HTML/CSS). `gzip` is stdlib; adds `Content-Encoding: gzip` + `Vary: Accept-Encoding` to `headers_common`. Big libraries make `/api/library` multi-MB; this is a real win with zero dependency cost. Measure with perf bench.
- Conditional GET is already implemented for files (`ETag`/`If-None-Match`); extend it to `/api/library` and `/api/theme.css` (theme already has etag; library currently uses no-store + full bytes). Add `If-None-Match` handling in `public_state_bytes` with the existing `_public_state_signature`.

### 1.5 State store hardening

- Bump schema to v5 only when a real change lands; otherwise leave v4 alone. Reserve v5 for: settings key whitelist (see 1.6), `state_format_version` marker, and per-game `updated_at` (useful for sync and for the UI's "last changed" facts).
- Add `JsonStateStore.snapshot(n)` rotating snapshots: keep the last 5 writes as timestamped copies in `backups/` (bounded by the existing save backup limit setting). This turns "recover from .bak" into "recover from 30 minutes ago". Storage cost is tiny at current library sizes; make it opt-out via a setting.
- Add an fsync-latency note: on some filesystems whole-file JSON writes get slow at very large libraries. Record `last_write_ms` in the state store and expose it in `/api/health` so users can self-diagnose.
- Recovery endpoint: add a dry-run mode (`?dry_run=1`) that reports what recovery would restore without applying it.

### 1.6 Settings whitelist and validation

- Today `_save_settings_locked` merges arbitrary keys. Add a `SETTINGS_FIELDS` registry (name, type, bounds, default) in a new `settings_schema.py`. Unknown keys are dropped with a warning (never silently stored), known keys are coerced and bounded.
- This is the one Phase 1 change with migration risk: preserve all existing valid keys; run the schema against a fixture of every key currently emitted by `collectSettings()` in index.html (there are ~40). Add a test that parses `collectSettings` output against the schema by grepping the HTML or by shipping the key list in the test fixture.
- Expose the schema at `/api/settings/schema` so the UI can render future settings generically instead of hand-writing a field per setting.

### 1.7 Secrets and credential handling

- Audit every place a token can leak: query-string token in media URLs (currently passed as `?token=`; acceptable for local, but ensure `Referrer-Policy: no-referrer` is already set, verified yes). Keep header-first auth for all fetch calls; only media/screenshot URLs may use the query token.
- Add a `Secrets` wrapper in `env_config.py` that reads env vars once, redacts on access, and logs a one-time notice when an integration credential is present but invalid (e.g., RA 401s).
- `/api/log` currently serves the redacted diagnostic log: confirm the redaction regexes cover JSON bodies with nested secrets (they cover the common shapes; add a test with 10 real-shaped secrets to lock it in).

### 1.8 Update pipeline

- `updates.py` is solid (HTTPS, trusted prefix, checksum). Add: signature verification if the release pipeline later signs artifacts (Phase 6); explicit handling of GitHub API rate limits (already uses token when present) plus a cached fallback response so offline users see "could not check" instead of an error toast; a last-checked timestamp persisted to settings so the UI can show "checked 2h ago".

### 1.9 Background jobs

- `job_manager.py` exists and is used. Add: job result retention (keep last N finished jobs for the diagnostics view), a `/api/jobs` listing, cancellation tokens respected by metadata/media workers (already partially present), and a jobs view in the UI showing running/queued/failed with retry buttons. This turns "something is happening in the background" into a first-class, inspectable surface.

### 1.10 Concurrency and shutdown

- Audited strengths: `ThreadingHTTPServer` + locks + bounded plugin subprocesses. Weak spots to fix:
  - Long-poll `/api/running` is polling today. Keep polling (it works at 1s cadence) but make the poll interval adaptive: 1s while a session is active, 10s when idle. Battery/CPU win on handhelds.
  - Graceful shutdown: `beforeunload` triggers a shutdown call; add a server-side SIGINT/SIGTERM handler that stops accepting connections, waits for in-flight requests (with a timeout), then finishes session threads. Test with a subprocess.
  - Session cleanup on crash: document and test the current behavior (process groups survive; on next start, orphaned sessions are not re-adopted). Decide: show "N sessions were interrupted" banner on next launch.

### 1.11 Phase 1 exit criteria

- [ ] Routes table in place, all 39 test files green with zero test edits.
- [ ] Error model live for top 20 handlers; `Request-Id` on all responses.
- [ ] `/api/v1/*` mounted; UI still on legacy paths (migration is Phase 2).
- [ ] gzip + conditional GET on library payload; perf bench shows the improvement.
- [ ] Settings schema + `/api/settings/schema`; unknown-key drop test.
- [ ] Job listing endpoint + UI jobs view.
- [ ] Coverage floor raised by 2 points from baseline.

---

## Phase 2: frontend architecture (weeks 5-7)

Goal: `index.html` becomes a thin shell over testable, buildable assets. This is the biggest structural change; it is also the one that makes all future polish cheap.

### 2.1 Buildless asset split (no new toolchain)

Constraint: the app must keep running from a checkout with zero install steps (`python3 web_app.py`). That rules out bundlers for the default path. Plan:

- `index.html` keeps the shell (head, `<dialog>` markup, `<script>` tags) and loads three local files: `static/app.js`, `static/app.css`, `static/views.js` (or a per-view split, see 2.2).
- `web_app.py` serves `/static/*.js|css` from disk, gzipped, with ETags. No build step needed.
- The split happens in slices: first move pure utilities (`escapeHtml`, `media`, `formatBytes`, `api`, `notify`, `setButtonBusy`), then state, then render functions, then dialog handlers. Each slice must leave the app running; this is a mechanical, reviewable migration, not a rewrite.
- When the split is done, add an optional minification step for the AppImage build only (`python -m zipfile`-style or a tiny script; no node dependency in the shipped artifact). Dev path stays unminified for debuggability.
- Add a `static/README.md` documenting the module order and the "no globals across files except via a single `App` namespace" rule.

### 2.2 State management

- Introduce an explicit `AppState` object (plain object, no framework) that owns: `games`, `playlists`, `settings`, `selectedId`, `filters`, `sessions`, `jobs`. All mutations go through `AppState.set(path, value)` which marks dirty flags per view.
- Views become `render()` functions that read `AppState` and write to a scoped container. Kill the current pattern of functions reading 20 file-global `let`s.
- Persist browser-only state (active filter preset, view mode, sidebar visibility) in `localStorage` with a versioned key and a reset path (add a "Reset UI" item in Settings for support).
- One render scheduler: `requestAnimationFrame`-batched render calls so three mutations in one handler produce one DOM pass. Measure with the existing perf browser script.

### 2.3 Router and dialogs

- Replace string-concatenated innerHTML in hot paths (grid rows, sidebar) with DOM building for the top 5 hot spots only; leave the long tail as-is. The goal is not "no innerHTML ever" but "no untrusted data in hot innerHTML" (escapeHtml already covers injection; the audit is about perf and listener leaks).
- Dialog manager: `openDialog(id, opts)` / `closeDialog(id)` wrappers that handle focus trap, Escape, `returnValue`, and scroll restoration uniformly. Migrate the 21 dialogs to it. Each dialog gets a `data-dialog-owner` so orphaned listeners are greppable.
- Add a global error boundary equivalent: `window.onerror` + `unhandledrejection` handlers that show a non-blocking error toast with the error message and a "copy diagnostic info" action (includes the `Request-Id` from Phase 1).

### 2.4 The UI moves to v1 routes

- Build the `API` map (single object: `library: '/api/v1/library', ...`) and migrate call sites in slices. Each slice verifies against a local server.
- When every call is on v1, keep the legacy paths for third-party API users (README documents them as legacy).

### 2.5 Real localization

Current state: a locale dropdown with 5 options and a 10-string `STRINGS` table in `parity_premium.py`; the rest of the UI is hardcoded English. Decide honestly:

- **Option A (chosen)**: ship English-only, remove the locale dropdown and the fake strings, document that localization is a post-1.0 feature. This is the honest choice; a 10-string fake i18n is worse than none.
- **Option B**: full i18n with a `strings.json` per locale and a script that verifies key parity across locales, plus a CI check that no new UI string ships without a key. This is a multi-week feature on its own.
- The plan recommends A for 1.0 (the codebase would need a string-extraction pass over ~2,000 JS lines and every template literal; do it right later or not at all).

### 2.6 Accessibility baseline

Target: WCAG 2.1 AA for the management UI; Big Box gets "operable by keyboard" as its floor (it is controller-first by design).

- Full `aria` pass: every icon-only button gets `aria-label` (the grid art buttons already do; the topbar does not consistently), every dialog gets `aria-labelledby`, live regions for toast + session state (`aria-live="polite"`).
- Focus order: sidebar -> grid -> detail pane; verify tab order matches visual order in the three-column layout. Trap focus in dialogs (2.3 wrapper does this).
- Keyboard: every action reachable by keyboard; document the shortcuts in a `?` help overlay (this doubles as discoverability).
- Contrast: DESIGN.md tokens are dark-on-dark in places; audit the 8-11px label sizes flagged by the design review. Raise the smallest labels to 12px where layout allows; contrast-check `--muted` against surfaces and bump if below 4.5:1.
- `prefers-reduced-motion`: verified present for the grid; extend to Big Box transitions and cover hover animations.
- Focus-visible styles everywhere; no `outline: none` without replacement.

### 2.7 Error and empty states

- Replace the generic `notify('Request failed')` surface with three levels:
  1. **Toast**: transient success.
  2. **Inline**: field-level validation errors (e.g., invalid launch command) rendered next to the field.
  3. **Banner**: persistent, dismissible, with a "Copy details" button for server errors, including `Request-Id`.
- Empty states: the design review already flagged the empty-library path (split import entry points). Build one onboarding surface: a single card with "Import from Steam / Heroic / Lutris / folder / manual" and a "just explore the empty library" escape hatch. Unify it with the welcome dialog.
- Loading states: every async action has a busy state (the codebase mostly does this via `setButtonBusy`); audit the remaining 20% and add skeletons for the grid on first load of a huge library.

### 2.8 Performance surfaces

- Virtualization already exists for the grid; verify it survives the 2.1 split with the perf browser script and add a 10k-game test fixture to `scripts/perf_gen_library.py` so the CI (or a manual gate) can measure render time.
- Image loading: `loading="lazy"` + `decoding="async"` already in use. Add `fetchpriority="high"` for the selected game's hero and `fetchpriority="low"` for grid covers.
- Media cache: the extraction/media cache under `DATA/cache` needs a size-bounded eviction (LRU by mtime with a configurable cap, default e.g. 2GB). Today it can grow unbounded. Add a "Cache" row in Settings with used size + a Clear button.
- `/api/library` payload: add `?fields=` filtering support so future views can request only what they need (keeps the full payload as default for compatibility).

### 2.9 Phase 2 exit criteria

- [ ] `index.html` under ~800 lines; `static/` contains the rest; app runs from checkout with no build.
- [ ] AppState in place; no view reads 20+ globals.
- [ ] All API calls on v1 paths; legacy paths still served and tested.
- [ ] Dialog manager covers all 21 dialogs; keyboard trap + Escape verified in a checklist.
- [ ] Error banner with Request-Id; onboarding unified; empty states for grid/search/playlists.
- [ ] A11y audit (automated via a checklist, not an external tool) passes for top 30 screens.
- [ ] Perf: 10k-game render under the target set in Phase 5's gate.
- [ ] Locale decision made and shipped (English-only with dropdown removed, or full i18n kicked to its own epic).

---

## Phase 3: design and product polish (weeks 8-9)

Goal: execute the design review's findings and bring the visual layer to "we paid a designer" level.

### 3.1 The command rail problem

The design review's P1: 24+ equally weighted commands before the library. Fix:

- Group the topbar into 3 zones: **Library** (search, view toggle, sort, filter), **Actions** (import, launch queue, random), **Tools** (settings, diagnostics, themes, etc.) in an overflow "More" menu.
- Give the library header the visual lead: title + count + active filter as the largest text on screen after the selected game.
- The topbar becomes context-aware: while a game is selected, "launch" stays reachable without scrolling.

### 3.2 Type and density

- Raise labels from 8-11px to a 12px floor where the grid tolerates it (per design review).
- Standardize detail-pane facts: one row layout with label/value pairs, consistent alignment, no mixed cases.
- Chips: consistent 3-state treatment (neutral/active/danger) across rating, ESRB, achievements.

### 3.3 Onboarding and first-run

- First-run: welcome dialog -> single import surface -> progress steps (found N games -> chose platform mapping -> done with a "launch your first game" call-to-action).
- Import errors: today a folder with zero recognized files shows a near-empty result. Add an explicit "nothing recognized here" state with a link to the supported-platforms docs page.

### 3.4 Big Box

- Controller focus order documented and tested (the gamepad map already exists in settings).
- Screen reader mode off by default but present: Big Box gets `aria-hidden` regions for decorative layers.
- Screensaver (already exists) gets a settings preview and a "test now" button.

### 3.5 Screenshots and brand assets

- Refresh `assets/` screenshots after 3.1-3.4 land (the README must never show an older UI than the release).
- Add a screenshot-capture script note to the release checklist (scripts already exist).

### 3.6 Phase 3 exit criteria

- [ ] Topbar regrouped; library header is the visual lead.
- [ ] Design review's P1s all closed (verified by re-running the same review process).
- [ ] Type floor raised; contrast audit passes.
- [ ] First-run flow tested end to end on a clean data dir.
- [ ] New screenshots committed; README/docs site updated to match.

---

## Phase 4: reliability and edge cases (weeks 10-11)

Goal: enumerate and fix the failure modes a real user base will hit in week one.

### 4.1 Edge case catalog (write it, then fix it)

Create `docs/reliability.md` with a numbered table: scenario, expected behavior, test or manual check. Minimum set:

**State & data**
1. `library.json` truncated mid-write (crash/power loss): `.bak` recovers; snapshot recovers older state; corrupt primary + corrupt backup -> explicit recovery dialog, never silent wipe.
2. Library on a full disk: atomic write fails -> error surfaces with the path and free-space hint; no partial file left.
3. Two OpenBox processes (user runs AppImage and source): filesystem lock behavior today; decide and test (second instance should detect and either exit or offer to take over).
4. State file is valid JSON but has a wrong schema version from the future (user downgraded): today `_validate_state` may reject; must produce "created by a newer version" message, not a 500 loop.

**Launch & processes**
5. Launching a game whose binary disappeared between grid render and click: tested path exists; verify the message names the missing path.
6. Game spawns children then exits immediately: finish_session must not kill unrelated processes; test with a script that backgrounds a child.
7. Game never exits and user closes OpenBox: session threads and process groups survive; document the orphan behavior in the UI ("game still running after OpenBox closed" is expected).
8. Two quick launches of the same game: double-click protection exists? Verify; add a "starting" state on the button with debounce.
9. Emulator install fails mid-download: temp cleanup, no half-installed emulator dir, retry works.
10. Game with a non-UTF8 filename in path: import survives, UI renders replacement chars, launch still works.

**Network & integrations**
11. Offline metadata sync: LBDB sync must not hang the UI; job shows "offline" state with retry.
12. Steam library path on read-only mount / Flatpak sandbox: import reports permission error with the actual path.
13. RA/EmuMovies credentials wrong: 401 surfaces as "check credentials in settings", not a generic error.
14. Webhook target down: retries with backoff (exists, verify), then a notification with the last error.
15. GitHub rate-limited update check: cached response, "checked 2h ago" (Phase 1.8).

**Media & archives**
16. A 4GB zip with 10k files: extraction cap exists (MAX_ARCHIVE_MEMBERS 25000); verify the error message and that the cache dir does not balloon.
17. Manual PDF inside a password-protected zip: finder returns None + the `_media_notes` message (PLAN.md part 2), no crash.
18. Duplicate covers from two metadata sources: dedupe endpoint exists; verify it reports counts per field.
19. A game whose media path is a broken symlink: `/api/media` must 404 cleanly, not traceback.

**UI**
20. Library of 20,000 games: grid virtualizes; sidebar counts compute once; search debounces.
21. Very long game names (300+ chars): ellipsis everywhere, no layout break.
22. User deletes the selected game while a dialog is open: dialogs must close or rebind without a stale `selectedId` crash.
23. Rapid filter switching during render: no half-rendered state; render scheduler (2.2) handles it.

### 4.2 Crash reporting

- Local-only, opt-in: an uncaught-exception handler already writes the traceback to the diagnostic log. Add a "Report an issue" button that packages (redacted log + system info + version) into a text the user pastes into GitHub. No automatic telemetry, no phone-home. This matches the product's local-first promise and SECURITY.md.

### 4.3 Support matrix

- Define and test: Ubuntu LTS (2 most recent), Fedora latest, Arch, SteamOS/Steam Deck, and "other glibc distro" as best effort. CI cannot test all; document the manual checklist per release.
- Python floor: 3.10 (CI already enforces). Browser floor: Chromium-family latest, Firefox ESR (document `--app-window` fallback behavior).
- Add a `SUPPORT.md` summarizing this and linking the docs site.

### 4.4 Phase 4 exit criteria

- [ ] `docs/reliability.md` exists with all 23 scenarios, each marked tested/known/documented.
- [ ] Every "tested" scenario has a test or a script in `scripts/`.
- [ ] Crash report packaging works and redacts secrets (verified against the secret shapes from Phase 1.7).
- [ ] SUPPORT.md published; release checklist includes the manual matrix run.

---

## Phase 5: performance program (weeks 12-13)

Goal: measured, reproducible numbers, with a gate that fails releases on regressions.

### 5.1 Baseline

- Run `scripts/perf_bench.py` on 1k / 5k / 10k / 50k games and record: library load ms, `/api/library` serialization ms, state write ms, grid first-render ms, scroll frame stability.
- Store results in `perf/results/` (gitignored) and a `PERF.md` summary with a table + date + machine spec.

### 5.2 Targets (candidate numbers; adjust after baseline)

- `/api/library` (10k games): under 200ms serialized, under 50MB memory peak.
- Grid first render (10k): under 500ms to first paint, 60fps scroll.
- State write: under 50ms for a 10k library on a SATA SSD.
- Cold start to UI ready: under 2s on the reference machine.

### 5.3 Optimizations in order

1. Serialization: `_build_public_state` clones everything; add field whitelisting for the public payload so private fields never cross, and measure `json.dumps` vs `orjson` (only adopt if the number justifies a dep).
2. State reads: the signature cache exists; verify `/api/running` and `/api/library` do not reload the full file per poll tick (they should reuse the cache).
3. SQLite read model: only if the JSON path misses targets at 50k. `metadata.py` already uses SQLite for LBDB; a separate `library.db` read index is a big change with sync complexity. Defer unless numbers demand it.
4. Frontend: DOM-building for grid rows (2.3), lazy sidebar counts, memoized filter results keyed by (filter signature, games fingerprint).

### 5.4 Gate

- CI runs the 10k benchmark once per PR (marked non-blocking first, blocking after two weeks of stable numbers).
- Releases include the perf table in the notes (release procedure already has a "Verification" section; add perf rows).

### 5.5 Phase 5 exit criteria

- [ ] PERF.md baseline published.
- [ ] All targets met or explicitly renegotiated with numbers.
- [ ] Perf gate in CI, blocking.
- [ ] Release notes include perf numbers.

---

## Phase 6: security and supply chain (weeks 14-15)

Goal: make "should I trust this binary" an easy question to answer yes.

### 6.1 Signed releases

- Sign the AppImage with a project Ed25519 key: `scripts/sign_release.sh` produces `OpenBox-x86_64.AppImage.sig`. The private key never touches CI (sign locally or via a secrets-managed signing step); the public key ships in the repo.
- `updates.py` verifies the signature before accepting an update, with a trusted-keys file that can rotate. Fallback: if a signature is unavailable (older releases), checksum-only with a visible "unsigned" warning in the UI.
- Document the signing key rotation and compromise procedure in SECURITY.md.

### 6.2 Dependency inventory (SBOM)

- The AppImage bundles a whole Python stdlib. Generate a per-release `sbom.json` (CycloneDX 1.4 minimum, generated by a small script that walks `runtime_modules.txt` + stdlib files + bundled libs). This satisfies "what is inside this artifact" with zero new runtime deps.
- Attach the SBOM to GitHub releases; mention it in the metainfo release entry.

### 6.3 Provenance

- Tag-to-artifact: add a CI step that asserts the release was built from the exact tag (already implied by `target_commitish`; make it explicit and fail on dirty trees).
- Publish build logs for AppImage releases (GitHub Actions artifacts) so users can verify what produced the binary.

### 6.4 Adversarial review of existing boundaries

- Re-run the hardening tests (`test_backend_hardening.py`, `test_secrets.py`, `test_packaging.py`) against the Phase 1 changes; add cases: token in logs after 401s, path traversal in `/api/media` with `..` and symlink chains (exists, extend), oversized multipart-less body (MAX_BODY exists, test the error), zip-slip in manual finder (PLAN.md's finder extracts to the cache root; add containment assertions).
- Add a `SECURITY-REVIEW.md` log: date, reviewer, scope, findings, fixes (lightweight, not a full pentest).

### 6.5 Phase 6 exit criteria

- [ ] Signed release published for the next version; UI verifies signature.
- [ ] SBOM attached to release; docs explain how to read it.
- [ ] Build provenance step green.
- [ ] SECURITY-REVIEW.md updated with the Phase 6 findings.

---

## Phase 7: docs, release trust, and community (weeks 16-17)

### 7.1 Release process automation

- Replace the remaining manual release steps with a `scripts/release.sh` checklist runner: verifies version sync (Phase 0.5), runs `make check`, runs the perf gate, builds the AppImage, signs, generates SBOM, drafts release notes from CHANGELOG's Unreleased section.
- Keep the human approval step for the final `gh release edit` per the existing convention (softprops overwrite behavior is already documented in memory/skills).

### 7.2 Documentation

- App repo: README gets a "Project status" section (stable, what's in progress, roadmap link), a support matrix link, and a clear "this is not affiliated with LaunchBox" statement (already present; keep it prominent).
- Docs site repo: add pages for reliability scenarios (from Phase 4), the API v1 contract (auto-generated from the routes table), and a "troubleshooting" index that maps error codes to fixes.
- Keep CHANGELOG.md as the single source of truth for release notes (it already is; make the release script consume it).

### 7.3 Community trust

- Issue triage: labels + a `TRIAGE.md` with response-time expectations (SECURITY.md already sets 5/14 days for security; add normal triage: first response within 7 days).
- Bug template gains the fields that actually matter (version, distro, log excerpt, library size) matching the crash-report packager.
- Funding/attribution: keep the Buy Me a Coffee link; ensure LICENSE headers are consistent across files (check for missing headers; AGPL text lives at repo root).

### 7.4 Phase 7 exit criteria

- [ ] `scripts/release.sh` runs the whole pipeline up to the human approval step.
- [ ] API v1 contract page generated from the routes table.
- [ ] TRIAGE.md and updated templates live.
- [ ] A release candidate goes through the full pipeline end to end.

---

## Rollout and tracking

### Sequencing rationale

Phases 0-2 are load-bearing: they convert "works on my machine" into "verified by machine". Phases 3-7 are polish on top of a structure that no longer breaks when you touch it. Skipping to pixels first would produce a prettier app that is still unsafe to change.

### Epic breakdown (suggested issue labels)

- `epic/engineering-foundation` (Phase 0)
- `epic/api-contract` (Phase 1)
- `epic/frontend-split` (Phase 2)
- `epic/design-polish` (Phase 3)
- `epic/reliability` (Phase 4)
- `epic/performance` (Phase 5)
- `epic/security-supply-chain` (Phase 6)
- `epic/release-trust` (Phase 7)

### Definition of done (every epic)

1. All tests green (`make check`).
2. Coverage floor maintained or raised.
3. CHANGELOG Unreleased entry added.
4. Docs updated where the epic touches user-visible behavior.
5. Perf numbers recorded when the epic touches hot paths.
6. No new runtime dependencies without a written justification in the PR.

### What this plan deliberately does NOT do

- No rewrite in a new language or framework. The single-file UI split is the biggest structural change, and it is a migration, not a rewrite.
- No cloud services, accounts, or telemetry. Local-first is the product.
- No Premium tier. Already excluded by PRODUCT.md.
- No test-framework migration mid-plan. The `test_*.py` convention survives to 1.0.
- No new feature epics (manual viewer, gamepad remapping UI, etc.) inside this plan; they ride after the foundation or not at all.

### Risks

1. **Frontend split is the riskiest step.** Mitigation: slice-by-slice, app must run after every slice, zero behavior changes per slice, tests for the API map before migration.
2. **Coverage floor gaming.** Mitigation: floor raises are small and reviewed; "test that asserts nothing" is rejected in review.
3. **Scope creep into features.** Mitigation: the de-scope list is part of the plan; feature requests get filed against post-1.0.
4. **Release signing key management.** Mitigation: key lives in the maintainer's local vault, rotation procedure documented before the first signed release.

### Suggested final acceptance for 1.0

- [ ] A release candidate passes `make check` + perf gate + signed AppImage + SBOM + auto-generated API docs.
- [ ] The 23 reliability scenarios are all tested or explicitly documented.
- [ ] The design review (re-run) scores the same surfaces 50+/60.
- [ ] A new contributor can clone, run `make check`, and open a PR that CI validates in under 10 minutes of setup.
- [ ] A user on a clean machine can go from download to first game launch with no terminal, no manual config, and no unexplained error.
