# OpenBox next-update plan

Planning baseline: September 8, 2026; local commit `b4e2810`; application version `1.9.0` in `updates.py`; working tree clean before this planning document was added.

This is an execution plan for one continuous, long-running implementation effort once execution is requested. Creating or editing this plan does not itself start that effort. Findings below distinguish isolated reproductions, source inspection, and proposed work. Public release availability, open GitHub issues, user demand, current distribution requirements, and real handheld behavior were not checked. Version numbers below are proposals based on the local version, and should be reconciled with intervening releases before implementation.

## Continuous execution contract

Execute the included work in one long looping run. Milestones are internal checkpoints within that run, not separate tasks that require the user to say “continue.” Carry the work from baseline verification through implementation, integration, recovery testing, documentation, and a locally validated release candidate. The patch and feature release split describes how changes are grouped and reviewed; it is not an instruction to stop after preparing the patch.

Use this loop throughout execution:

1. **Read the checkpoint and inspect current state.** Reconcile the working tree, completed work, test evidence, remaining acceptance criteria, and any new user steering. Preserve unrelated user changes.
2. **Choose the next concrete unmet requirement.** Follow priority and dependencies. Prefer corrective work before dependent UI. Resolve ordinary implementation decisions using the defaults in this plan and repository conventions.
3. **Implement a bounded, reviewable change.** Keep modules, contracts, themes, translations, runtime registration, and relevant documentation consistent. Do not start an unrelated feature because the current requirement is difficult.
4. **Verify the behavior.** Add meaningful regressions for corrected failures and run focused checks. Before marking a milestone complete, run the appropriate integration checks and the full gate. Enforce at least 95% changed-line coverage; never lower a floor to clear a failure.
5. **Repair and repeat when checks fail.** Diagnose the cause, fix it within scope, and rerun affected checks. Record unrelated baseline or environmental failures accurately; do not report a passing gate while failures remain.
6. **Review against acceptance criteria.** Compare actual behavior with the promised workflow, including failure, concurrency, recovery, and compatibility cases. A passing unit test alone does not complete an end-to-end requirement.
7. **Save a checkpoint and advance immediately.** Update `docs/NEXT_UPDATE_EXECUTION.md` with completed criteria, changed files, commands/results, open defects, decisions, and the next action. Provide concise progress updates while continuing to work.
8. **Repeat until the included release scope is complete.** After all workstreams are integrated, run the release-candidate checks and correct any newly exposed defects through the same loop.

Do not end the run merely because one milestone is complete, one test run passes, a proposed estimate has elapsed, or a context boundary is approaching. After context compaction, resume from the saved checkpoint without repeating completed work unnecessarily. This is sustained implementation within the current task; it does not require a recurring automation or additional user-owned tasks.

If a step is blocked by unavailable hardware, credentials, an external service, or a decision with no defensible default, record the exact blocker and continue independent work. Ask only for information or authorization actually required to unblock the dependent step. Do not spin on an unchanged blocker, invent test evidence, or mark the affected requirement complete. If all remaining work is blocked, return a precise checkpoint describing what is complete and what input or environment change is needed to resume.

Apply the scope-control rules in this plan deliberately and record the reason for each permitted deferral. Do not silently downgrade a core requirement to finish sooner. Publishing releases, submitting to Flathub, and sending external messages remain subject to explicit authorization; prepare concrete artifacts and review evidence first, while completing every other authorized step that can proceed.

The run finishes successfully only when all included acceptance criteria are satisfied, required checks have passed, documentation matches the result, and the release candidate is ready for the remaining authorized release actions. The final response should report delivered behavior, verification evidence, intentional deferrals, and any genuine remaining limitations.

## 1. Recommended direction

Make the next substantial release **1.10.0: reliable libraries, easier migration, and complete everyday workflows**. First prepare a narrowly scoped **1.9.1 corrective release** for confirmed correctness problems.

OpenBox already has substantial breadth: store and ROM imports, emulator setup, metadata, saves, session tracking, a controller interface, playlists, insights, recommendations, a relationship graph, themes, localization, and signed packaging. Reintroducing these as new features would make an inaccurate roadmap. The strongest opportunity is to make the recently added capabilities dependable and accessible from the normal UI.

The release promise should be concrete:

> Bring your library into OpenBox, understand what will change, keep each device's launch setup intact, and reliably get into your next game.

Priorities, in order:

1. Prevent unintended loss or replacement of library information.
2. Make previewed actions agree with committed actions.
3. Complete the migration and shelf-entry experiences already backed by APIs.
4. Make launch behavior predictable with mouse, keyboard, and controller.
5. Prove that optional acceleration preserves behavior before expanding it.
6. Validate the installed application, including translations, rather than only the source tree.

Do not make a wholesale framework migration, a new database write architecture, or another large discovery dashboard a prerequisite for this release.

## 2. What exists, and what the inspection actually established

| Area | Existing capability | Evidence and consequence for this plan |
|---|---|---|
| Current release baseline | Local version and changelog identify 1.9.0 | `updates.py`, `docs/CHANGELOG.md`; do not describe 1.9 features as new work |
| Full library sync | Publish and pull routes, a mounted-folder snapshot, timestamp conflict handling, tombstone consumption | `cloud_sync.py`, `handlers/health.py`, ADR 0035; correctness needs work before greater exposure |
| LaunchBox migration | XML parser, preview/apply routes, emulator ID report | `pkg/parity/parity_launchbox_import.py`, `handlers/imports.py`, ADR 0033; complete and reconcile the workflow |
| Shelf entries | Name-only creation route sets `manual_entry: true` and empty path | `handlers/library.py`, ADR 0036; creation exists but UI differentiation was explicitly deferred |
| SQLite | Optional read projection, server search, facet integration | `pkg/state/sqlite_readmodel.py`, `handlers/library.py`, ADR 0032; JSON remains canonical and `/api/library` remains JSON |
| Large-library UI | Existing virtualization and search worker | `static/library.js`, `static/worker.search.js`, `docs/development/PERF.md`; benchmark and improve these rather than proposing them as new |
| Long operations | Existing durable operations and Activity UI | `pkg/state/operations.py`, `static/activity.js`; reuse this system for new long workflows |
| Controller and video | Big Box modes, gamepad navigation, video snaps and reduced-motion behavior | `static/bigbox.js`, `static/navigation.js`, ADR 0034; focus on lifecycle and failure tests |
| Localization | Five locale files and a key-coverage checker | `scripts/check_i18n.py`; key coverage does not prove all visible strings are translated |
| Distribution | x86_64/aarch64 release work, Flatpak manifest and bundle workflow | CI and release workflows; Flathub submission remains separate from bundle preparation |

### Confirmed by isolated execution

**F1 — Deletion propagation is incomplete.** In a temporary directory, publish a library containing one game, publish the same library after removing it, then pull onto a device that still has it. The resulting remote tombstone map is empty, the pull deletes zero games, and the other device retains the game. `publish_library()` preserves existing tombstones but does not generate new ones from local deletion events. The published documentation promises more than this sequence delivers.

**F2 — Pull replaces device-local data and edits.** A remote entry with `/device-a/quake` replaced the receiving entry's `/device-b/quake` path and removed its local favorite field in an isolated pull. Publication gives every game the current publish timestamp; pull replaces the whole matching game object when that timestamp is newer than `_sync_updated_at`. A publish time is not sufficient evidence that every field is the newest edit.

**F3 — SQLite facet identity checks do not establish semantic parity.** A fixture with one visible game in `Action, Adventure` and one hidden game in `Puzzle` passed `query_parity_check()`. The JSON facets returned separate `Action` and `Adventure` entries; SQLite returned `Action, Adventure` and `Puzzle`. The SQL facet path does not currently apply the same hidden-game and genre-splitting behavior. An identical game-ID set does not prove equivalent facet results.

### Confirmed by source inspection; add behavioral regressions before fixing

**F4 — Migration preview and apply use different decisions.** Preview checks existing LaunchBox IDs and lowercased names. Apply reparses and delegates to `merge_imported_games()` using `game_identity()`, whose fallback is the path. Preview does not use the same merge-policy decision as apply. Test cross-platform same-title games, duplicate rows, changed paths, and import exclusions before promising exact preview counts.

**F5 — Migration source identity and metadata identity are mixed.** The XML parser maps `<ID>` to `launchbox_db_id`. Existing metadata code also uses `launchbox_db_id` as the metadata database match identifier. The application contains two different uses under one field. Validate representative XML fixtures and separate these identities without silently rewriting ambiguous historical data.

**F6 — Shelf semantics do not reach the usual UI model.** The creation route writes `manual_entry`, but a repository search found no handling of that field in the normal game projection or frontend. `pkg/state/cache.py` computes `path_exists=False` for an empty path. Trace the complete HTTP/UI flow and define catalog-versus-launchability behavior before adding a form that exposes this broadly.

**F7 — Pull has a stale-state replacement window.** `handlers/health.py` reads local state, computes a result outside the transaction, then assigns that entire games list inside `transact_state()`. A concurrent local mutation between those steps can be replaced. Add a deterministic interleaving test.

**F8 — System-install locale packaging appears incomplete.** The inspected `Makefile` copies static files, themes, emulator definitions, and runtime modules but has no locale installation step. AppImage and Flatpak packaging do explicitly copy locales. Reproduce with a staged `DESTDIR` installation and test installed locale endpoints.

**F9 — Search behavior depends on acceleration.** The v2 JSON fallback matches title substrings; SQLite searches a broader set of fields through FTS5 or LIKE. Search response handling also needs explicit lower bounds for `limit` and a consistent empty-query shape. Define the intended v2 search semantics and validate its boundary cases; do not assume it is the same search engine as the browser worker.

**F10 — Rapid launches have no documented deduplication guarantee.** `docs/reliability.md` explicitly says there is no launch-dedupe guard. Decide the interaction contract and test it across all launch entry points.

### Verification performed for this plan

- `python3 -B tests/test_library_sync.py`: 14 tests passed.
- `python3 -B tests/test_launchbox_import.py`: 9 tests passed.
- `python3 -B tests/test_manual_entry.py`: 5 tests passed.
- Three disposable reproductions established F1, F2, and F3.
- No full `make check`, browser walkthrough, performance benchmark, package build, or hardware test was run for this planning task.

Passing these 28 tests is evidence about those test cases, not proof that the workflows above are correct. The corrective release should add regressions for the missing scenarios.

## 3. Release split and scope control

### Proposed 1.9.1 corrective release

Mandatory candidates:

1. Reproduce and correct unsafe sync merges and missing deletion handling; if a correct compatible repair is larger than a patch, make affected operations fail clearly without mutating data until the replacement is ready.
2. Restore JSON-equivalent facets when SQLite is enabled. Routing affected facet queries through the established JSON implementation is a valid short-term fix.
3. Make migration preview/apply policy consistent and resolve the source-ID collision before encouraging migration at scale. Restrict unsupported cases with useful errors if a complete repair cannot be contained in a patch.
4. Verify and fix the staged-install locale omission.
5. Correct documentation and add targeted regressions for every included fix.

A patch must not quietly introduce a new distributed sync protocol, reinterpret all historical IDs, or broaden the UI. Prefer a safe, explicit limitation over maintaining an inaccurate success claim. Preserve route registration and existing v1 contracts.

### Proposed 1.10.0 core

- Safe sync engine and a reviewable two-device workflow, if the engine passes all convergence and recovery checks.
- Guided LaunchBox migration with path mapping, accurate preview, and import results.
- Shelf-entry creation/editing, clear launchability, and conversion to a playable local entry.
- Duplicate launch protection and consistent launch-state feedback.
- Search/facet correctness plus measured performance work.
- Keyboard/controller, translation, packaging, and release regression coverage for the changed workflows.

### First items to defer when scope grows

1. New Wrapped, Mastery, Constellation, or recommendation features.
2. Bulk physical collection inventory features.
3. Automatic background library sync and media-file replication.
4. Advanced metadata provenance across every provider.
5. Default-on SQLite or a server-paginated replacement for the existing library frontend.
6. Flathub submission, which has its own maintainer and external-review dependencies.

Keep the sync UI out of a release if its engine is not ready. Migration, shelf entries, and launch reliability can still form a useful 1.10.0 after the corrective release has made the unsafe sync behavior explicit.

## 4. Workstream A — Safe, understandable library sync

**Priority:** P0 correctness; P1 UI. **Risk:** high. **Likely implementation:** existing `cloud_sync.py`, `handlers/health.py`, state transaction/deletion paths, settings schema, and existing operations infrastructure. Keep new integration helpers under `pkg/parity/` or `handlers/` as appropriate; do not add new root modules.

### Product behavior

Settings should distinguish statistics sync from library sync. The library section shows the chosen folder, device name, last successful result, and actions to preview incoming changes and publish local changes. Users see counts and examples of additions, updates, deletions, conflicts, and unresolved local launch paths before applying incoming changes.

Example: a game added on a desktop appears in the handheld catalog after pull. The handheld's existing executable location remains intact. A new game with only a desktop path is visibly unresolved on the handheld; OpenBox does not pretend the remote path is usable.

### Engine design tasks

1. Write an ADR defining record identity, device identity, fields, revisions, deletion semantics, compatibility, and failure recovery before changing the protocol.
2. Persist a unique device identity; a caller-supplied default string `local` is insufficient for reliable multi-device attribution.
3. Classify fields into shared catalog metadata, device-local launch/install configuration, statistics with their existing policy, and local/transient state. Review plugin/custom fields explicitly. Sync neither credentials nor executable commands by accidental whole-object copying.
4. Decide how independently imported copies of the same game are linked. Preserve stable local IDs and use provider identities or an explicit reconciliation step. Do not auto-merge unrelated same-title games.
5. Record real local edits and deletion events transactionally. Publishing an unchanged record must not manufacture a new edit revision. Deleting an entry must produce a durable tombstone.
6. Keep a last-seen base or equivalent revision information so a stale device cannot overwrite a newer edit simply by publishing later. A base/local/remote comparison with explicit conflicts is a reasonable minimum; prove it under concurrency rather than assuming timestamps suffice.
7. Define edit-versus-delete and re-add behavior. Clear a tombstone only for an intentional later restoration, never merely because an offline device still has an old entry.
8. Treat `flock` as local filesystem coordination. Do not assume a lock file coordinates separately replicated folders across computers. Evaluate per-device files and deterministic merge versus a versioned shared-file design; test the actual intended transport model.
9. Read and parse external files outside the state lock, then compare/recompute the merge against current state inside the commit boundary. Reject stale previews or recompute them explicitly.
10. Validate format versions, object shapes, sizes, duplicate identities, malformed timestamps, and conflicting records. Reject or quarantine invalid input without overwriting the local library or the invalid source file.
11. Save a local recovery snapshot before a destructive apply. Retention and recovery must be documented and tested with disk-full failures.
12. Preserve previous-format files until compatibility is proven. Do not let an old client overwrite a new format silently. Use a separate versioned file if necessary.

### UI and operation integration

- Use the existing Activity drawer for operations that take time; report phase, counts, last error, cancellation availability, and retry eligibility.
- Keep file I/O and parsing out of the central state lock. Make the final commit atomic and clearly identify any non-cancellable commit phase.
- Allow per-conflict local/remote selection with a field-level explanation. Keep the first version manual; automatic schedules can follow after correctness is established.
- Offer local path resolution separately from metadata synchronization.
- Explain that synchronizing catalog records does not copy ROMs, installed games, saves, or media files.

### Acceptance tests

- A adds, B pulls: exactly one corresponding entry, and repeated pulls add nothing.
- A deletes, B pulls: deletion arrives once and remains deleted after B publishes stale state.
- A edits metadata while B edits its local launch path: both intended changes survive.
- A and B edit the same shared field from a common base: conflict is visible and neither edit disappears silently.
- A publishes unchanged data: existing record revisions do not advance.
- A and B publish concurrently through independently replicated folders: the chosen format converges after exchange, with no lost entries.
- Local favorite/session/import updates during pull survive commit.
- A newer edit versus an older tombstone follows the documented policy.
- Invalid remote JSON, unsupported version, missing mount, permission error, disk-full write, and interruption leave recoverable local state.
- Preview does not mutate data; apply rejects a changed base or produces a new review.
- Previous-version clients cannot damage a new-format sync folder.

**Done means:** a two-device end-to-end test plus all conflict/recovery cases pass, and docs describe only the demonstrated behavior.

## 5. Workstream B — Guided LaunchBox migration

**Priority:** P0 policy repair, P1 workflow. **Risk:** medium-high. **Likely implementation:** `pkg/parity/parity_launchbox_import.py`, `handlers/imports.py`, `pkg/state/imports.py`, `static/imports.js` or existing Setup Center components.

### User journey

1. Open Import → LaunchBox migration.
2. Select one XML file; multi-file/platform-directory import is a later increment if it materially complicates the first release.
3. See recognized games, malformed entries, unsupported fields, and unresolved paths.
4. Map Windows or relative path roots to local Linux directories. Preview original and mapped paths without executing anything.
5. Review emulator references and explicitly map them to existing profiles or leave them unresolved.
6. Inspect the exact add/merge/skip decisions and fields affected.
7. Apply once; see progress, results, unresolved items, and a route into Launch Doctor.

### Implementation tasks

- Separate imported library-record identity from the LaunchBox metadata database match ID. Use a dedicated source identifier or the existing source-identity mechanism after checking its semantics.
- Audit actual export fixtures before promising field coverage. Current `_FIELD_MAP` is intentionally limited. Check destination names for manual/music/video paths against OpenBox's normal schema and media normalization.
- Build one pure import-plan function consumed by both preview and apply. Include intra-file duplicates and the existing import exclusion policy.
- Deduplicate by verified source identity and existing canonical/provider rules. Treat title similarity as review evidence, not a universal merge key.
- Bind preview to source content, mapping options, and library revision; detect XML edits or local library changes before applying.
- Support Windows drive roots, backslashes, relative paths, spaces, Unicode, missing paths, and deliberate platform changes. Do not infer emulator commands from arbitrary XML text.
- Preserve user-owned metadata by default; show explicit overwrite choices for fields the import can replace.
- Bound input size and parsing memory. For large files use stdlib streaming parsing if measurements show full-tree parsing is unsuitable.
- Stage a recovery copy and commit through existing transaction/operation facilities. Avoid downloading artwork while holding the library lock.
- Start with existing local media references; large folder copies and cross-machine media migration can be separate jobs later.

### Acceptance tests

- On an unchanged base, preview add/merge/skip decisions equal committed results.
- A same-title game on two platforms is not collapsed by name alone.
- Reimporting the same file is idempotent, including when it contains duplicate rows.
- A known source record whose path changed follows the documented update/review policy.
- Two pathless imported games retain distinct identities or are explicitly rejected for review.
- Metadata matching after migration still uses valid metadata IDs.
- An invalid XML document produces a useful error with zero partial library mutation.
- A changed preview source/base is detected.
- Cancelling before commit leaves no partial library; retry after commit does not duplicate entries.
- One representative migrated playable game passes Launch Doctor and launches on Linux with an explicitly chosen emulator/profile.

**Done means:** a user can complete a supported migration from the UI without issuing API requests, and the final report is trustworthy.

## 6. Workstream C — Finish shelf entries

**Priority:** P1. **Risk:** medium because path assumptions are widespread. **Likely implementation:** `handlers/library.py`, `pkg/state/cache.py`, `static/dialogs.js`, `static/library.js`, `static/state.js`, launch checks and relevant analytics/picker modules.

### Product behavior

Offer an Add game flow with a shelf-entry option for games without local files. Require a title, allow existing metadata fields, and label the entry clearly. The primary action for a shelf entry should make sense: Details, Edit, or Set up launch. It should not present an ordinary Play action that inevitably fails because no file exists.

### Implementation tasks

1. Expose catalog-entry type through a reviewed public projection. Check the v1 response contract before adding fields to shared projections; use an additive v2 surface if the contract requires it.
2. Separate intentional non-playable entries from missing installations and broken launch configurations.
3. Add create/edit handling that preserves the entry flag and stable ID on save and reload.
4. Add an all/playable/shelf filter without breaking existing installed/owned filters, saved presets, or hash routing.
5. Audit library health, Launch Doctor, bulk edit, export, sync, search, picker, Game Night, and Big Box for empty-path assumptions.
6. Allow conversion to a local playable game while preserving title metadata, tags, playlists, notes, history, and identity. Specify whether removing a path converts back or requests a separate explicit action.
7. Decide analytics denominators: shelf games may count toward collection size but should not distort installed/playable counts. Existing recorded play history can remain meaningful.

### Acceptance tests

- Create, reload, edit, search, filter, export, and back up a shelf entry successfully.
- Two different pathless shelf entries remain distinct through save, restore, and sync.
- A shelf entry is not classified as a broken installation merely because its path is empty.
- Picker/Game Night launch actions do not attempt to execute it.
- Convert an entry to playable and launch it; playlists and metadata retain the same identity.
- Keyboard and controller users can complete the flow and return focus to its originating control.

Do not add lending, purchase-price history, barcode scanning, collection valuations, or a full board-game session subsystem in the core release.

## 7. Workstream D — Launch and controller reliability

**Priority:** P1. **Risk:** medium. **Likely implementation:** `pkg/state/launch.py`, `handlers/launch.py`, `static/sessions.js`, `static/bigbox.js`, `static/navigation.js`.

### Core change: one intentional launch

- Reserve a launch attempt atomically using stable game identity and a documented launch-profile policy before spawning the process.
- Repeated clicks, keyboard activation, controller events, or request retries while launch is pending must not create accidental duplicate sessions.
- Clear reservations on every failure path. Preserve existing process supervision, recovery, and lock-order rules.
- Decide how an intentional second instance works. An explicit action may be supported, but it must not be the default result of a double-click.
- Preserve frozen v1 behavior where required; introduce a reviewed v2 launch contract for richer conflict/idempotency responses if necessary.
- Render pending, running, failed, and finished states consistently across grid, detail, picker, and Big Box. Focusing an existing window is best-effort and must not be claimed on unsupported environments.

### Couch workflow checks

- Open Big Box, choose a game, launch, return, and continue from the previous selection without a mouse.
- A dialog takes controller ownership while open; background navigation must not also react to the same input.
- Focus survives filtering, virtualized scrolling, game deletion, a failed launch, and returning from a running game.
- Hot-unplug/reconnect a controller and retain keyboard escape/navigation.
- Video snaps do not start for a previously selected game after fast navigation. Failed playback and unsupported codecs fall back cleanly.
- Stop or detach inactive media work on mode exit; restore BGM state after video failure, launch, and return. Respect reduced motion and user audio settings.

### Acceptance tests

- Two simultaneous launch requests produce one process/session by default.
- A failed spawn can be retried successfully.
- Removing/reordering a game during launch cannot redirect the launch or playtime to another entry.
- A process that hands off to a child retains documented tracking behavior.
- Shelves and unresolved remote paths get appropriate setup feedback, not misleading launch attempts.
- Actual native WebKitGTK and a real handheld are included in release testing; browser-only automation does not prove these environments.

## 8. Workstream E — Correct search and measured scale

**Priority:** P0 facet correctness; P1 targeted measurement; P2 broader acceleration. **Risk:** medium.

### Semantics before optimization

Define a test corpus covering hidden games, comma-separated genres, blank platform/progress values, supported facet fields, ties in ordering, Unicode, punctuation, multiword search, substring expectations, malformed FTS expressions, and result limits.

For existing facet endpoints, SQL results must preserve the established JSON contract. Compare actual values/counts/order, not just IDs. If SQL cannot reproduce a field's semantics yet, use JSON for that field.

For v2 server search, document whether the API promises substring matching, token matching, or a selected mode. Decide what fields are searched and provide a consistent fallback. Validate minimum/maximum limits and empty-query responses. Do not silently change browser search semantics as a side effect.

### Measurement plan

Use synthetic fixtures at 1k, 5k, 10k, and 20k games. Treat 50k as exploratory until measured support is justified. Include long titles, sparse metadata, hidden games, multiple genres, large histories, missing files, and typical media references.

Measure separately:

- Cold startup to server readiness, first visible library, and first usable interaction.
- Warm search input to stable results and filter selection to visible update.
- SQL disabled/enabled, cold rebuild, warm query, and first query after a write.
- Favorite mutation, bulk edits, imports, and sync commit time.
- Browser long tasks, memory, rendered card count, and focus correctness under scrolling.
- JSON/SQLite allocations and projection work; count expensive rebuilds per operation.

Collect raw samples, p50/p95, fixture size, Python/browser versions, hardware, and cold/warm conditions. Do not reuse historical `PERF.md` figures as current measurements.

### Proposed performance goals, subject to baseline calibration

- Warm search/filter interaction: aim for p95 below 150 ms at 10k games on an explicitly recorded reference system.
- Cold usable UI: retain the documented under-2-second reference-machine aspiration, but measure the full UI path separately from server readiness.
- Retain existing 10k/20k write and API gates without weakening them. Prefer measured improvement over adding broad exceptions.
- At 20k, ensure DOM size stays bounded under scroll and selection remains usable; do not claim 60 FPS without a browser trace on the target device.

CI currently runs `perf-20k` with `OPENBOX_PERF_CI_MULT=2.5`; report base budgets and effective CI budgets distinctly. Five-run CI smoke measurements catch large regressions but are not sufficient for a robust performance characterization.

Investigate incremental SQLite updates, cached semantic parity per revision, and avoiding unnecessary full projections only after profiling shows which cost dominates. Keep the read model optional for this release unless correctness, rebuild cost, and operational recovery justify a separate default-on decision.

## 9. Workstream F — UI quality, translations, and installed behavior

**Priority:** P1 for changed flows and packaging; P2 for a whole-application sweep.

- Reuse existing dialogs, Setup Center patterns, Activity UI, and design tokens. Avoid introducing a second navigation or job system.
- Add every new visual value to `static/app.css :root` and all five themes as required by `AGENTS.md`.
- Translate labels, validation, empty states, errors, status text, and accessible names. Review remaining hardcoded strings in touched modules.
- Test live language switching while a dialog is open and after async content arrives. The key checker only discovers supported key references; it does not discover all untranslated English text or verify translation quality.
- Verify narrow layouts at 1280×800 and 1280×720, desktop at 1920×1080, and increased text/browser zoom. Check real scaled native behavior as well as browser viewport emulation.
- Check focus visibility, modal focus containment/restoration, screen-reader names, sufficient contrast in every stock theme, and reduced motion.
- Test system installation into a disposable `DESTDIR`, including all locale assets and locale HTTP responses served from the installed tree.
- Exercise AppImage and Flatpak builds from installed artifacts, not repository-relative paths. Verify first run, import, locale change, media access, one launch, restart, and update/recovery flows as appropriate.
- Reverify actual runtime/native-host compatibility during release preparation; manifest validation or a dry run does not prove a successful runnable package.

Do not add a new frontend runtime framework solely to implement these workflows. Development tooling can remain separate from the dependency-free Python runtime.

## 10. Optional follow-on improvements

These are candidates after core work, not promises for 1.10.0.

| Candidate | Why it may help | Minimum useful scope | Release rule |
|---|---|---|---|
| Metadata overwrite review | Helps preserve hand-edited descriptions and artwork | Clear current/proposed differences for touched migration/scraper fields | Prefer incremental reuse of existing match review |
| Backup diff UI polish | Makes existing backup/diff capability easier to understand | Added/removed/changed summary before restore | Verify existing UI first; do not claim the API is new |
| Launch Doctor repair guidance | Reduces dead-end errors | Explain missing path, emulator, BIOS, or profile and link to existing settings | No silent downloads or command rewrites |
| More informative recommendations | Helps users trust picker results | Show missing metadata and distinguish estimated time from observed history | Do not add cloud or AI dependencies |
| Game Night suitability overrides | Multiplayer metadata can be incomplete | Explicit local suitability/player-count overrides | First verify how current metadata/editing already supports this |
| Flathub submission | Adds a distribution channel | Complete artifact/runtime checks and prepare submission materials | Separate maintainer decision; external approval date is not ours to promise |

Require a demonstrated user problem, a bounded implementation, and acceptance criteria before promoting any of these into core scope.

## 11. Internal milestone sequence for the continuous run

Estimates are planning ranges for focused engineering days, including implementation and focused tests. They are not measured velocity, do not assume multiple developers, and exclude waiting for hardware or external reviews. They are not run-duration limits or instructions to split execution into separate sessions. Re-estimate after the baseline and sync-design milestones, then continue the loop.

| Milestone | Deliverable | Dependencies | Rough effort |
|---|---|---|---|
| M0 | Regressions for findings, full baseline gate, installed-tree check, prioritized decisions | None | 2–4 days |
| M1 | Contained 1.9.1 corrections or safe rejection of unsupported unsafe cases, patch notes | M0 | 3–7 days |
| M2 | Sync ADR, field ownership, compatibility, merge/recovery engine and tests | M0; build on M1 | 8–15 days |
| M3 | Migration policy/identity fixes, path mapping, guided UI and outcomes | M0/M1 | 5–9 days |
| M4 | Shelf projection, edit/filter/conversion flows and downstream checks | M0; coordinate identity with M2/M3 | 3–6 days |
| M5 | Launch reservation/feedback and controller/video lifecycle regressions | M0 | 3–6 days |
| M6 | Sync preview/conflict UI using the proven engine | M2 | 3–5 days |
| M7 | Search/facet semantics and measured targeted performance improvements | M1 | 3–6 days |
| M8 | Cross-feature integration, locale/theme/package/hardware checks, release candidate | Completed included workstreams | 5–8 days |

The sum is approximately **35–66 focused engineering days** before contingency. With 20–30% allowance for integration discoveries, the full proposal is approximately **42–86 days**, or roughly **9–18 working weeks for one full-time developer**. This is deliberately a range; shipping fewer complete workflows is preferable to treating the optimistic end as a deadline.

For a smaller update, complete M0/M1, migration, shelf entries, launch protection, and essential release checks; defer the sync UI/engine expansion and speculative performance changes. Re-estimate that smaller scope explicitly instead of compressing the whole plan into the same date.

### Reviewable change sequence within the same run

1. Add targeted regressions and an evidence ledger without implementation changes.
2. Fix each confirmed patch issue in its own focused change.
3. Agree on sync/source-identity ADRs and format compatibility before dependent UI work.
4. Land migration decision logic and fixtures, then its UI.
5. Land shelf projection/edit support, then conversion and downstream behavior.
6. Land launch reservation and failure-path tests, then shared UI feedback.
7. Land sync engine behind explicit opt-in, then preview/conflict UI after engine acceptance.
8. Land measured search/performance changes independently.
9. Integrate docs, artifacts, compatibility testing, and release notes against the actual final scope.

Use conventional commits scoped to the relevant module when committing work. Every PR must pass `make check` locally under project conventions; do not save validation until the release candidate. Prepare these changes sequentially within the same continuous execution loop; finishing a change or preparing review material is not a routine pause for user confirmation.

## 12. Test and release matrix

### Mandatory automated checks

- Existing `make check` stages: lint, runtime-module registry, frozen v1 contracts, version synchronization, frontend checks, i18n, compilation, coverage-backed tests, coverage thresholds, and token gate.
- Preserve the current script floors: 72% total, 54% `web_app.py`, 95% changed-line, and 85% new runtime module coverage.
- CI additionally runs `scripts/check_changed_coverage.py --fail-under=95`; both local and CI changed-line floors are now 95%. Preserve this requirement throughout the update.
- Register every new runtime module in `runtime_modules.txt` and add its standalone mirrored test file.
- Add real HTTP cases for new/modified routes: authentication, payload validation, errors, response shape, state mutation, and request-body consumption. Direct mocked-handler tests alone are insufficient.
- Add browser flows for migration preview/apply, shelf create/edit/convert, launch failure/retry, sync review/conflict resolution, and locale switching. Use observable completion conditions rather than fixed sleeps where practical.
- Run the existing blocking 10k/20k performance job and investigate regressions on comparable fixtures.

### Data and recovery matrix

Use disposable data directories and synthetic or sanitized fixtures; never use a personal library as a destructive test target.

| Dimension | Required cases |
|---|---|
| Library size | Empty, one game, 1k, 10k, 20k; 50k exploratory |
| Entry type | Steam/provider, local executable, ROM, missing install, shelf, mixed library |
| Identity | Same title across platforms, duplicate source rows, changed path, independently imported same game |
| State changes | Concurrent favorite, import, deletion, session completion, sync pull |
| Storage | Missing folder, read-only folder, disk-full write, invalid JSON/XML, interrupted operation |
| Locale/theme | All five locales and themes for changed UI; live locale switch; increased text size |
| Input | Mouse, keyboard, controller, controller reconnect, escape/back flow |
| Runtime | Python 3.10 and 3.12 as in current tests; x86_64 and aarch64 build/gate paths |
| Host | Browser UI and native WebKitGTK; actual supported handheld session |
| Upgrade | 1.9.0 fixture to new release, unknown fields retained, previous-format sync handling, recovery copy |

### Release candidate procedure

1. Freeze included features and update the plan to the actual scope.
2. Run the full gate and CI jobs on the release candidate commit.
3. Build both AppImage architectures and the Flatpak artifact; verify installed behavior and signing/update checks using the existing release tooling.
4. Complete the documented manual reliability scenarios that apply to changed code, including rapid launches, stale selection, offline operations, and controller return from game.
5. Perform the two-device sync walkthrough only if sync is included; archive sanitized inputs and outcome evidence.
6. Confirm recovery from pre-update backups. Do not promise backward downgrade compatibility unless it was tested against any changed formats.
7. Update changelog, release notes, parity matrix, reliability matrix, performance report, version metadata, and screenshots where needed.
8. Describe limitations honestly: supported migration shapes, whether sync is manual/opt-in, which data is local, and how optional SQLite differs or falls back.
9. Complete maintainer release review and publish through the established process. No publishing or external submission is authorized by this planning document itself.

## 13. Decisions to settle during M0

Resolve these decisions inside M0 using the recommended defaults and inspected evidence. Record the decisions in the execution checkpoint and relevant ADRs, then continue immediately. Ask the user only when required information or authorization is missing; do not turn this table into a mandatory approval checklist.

| Decision | Recommended default | Evidence needed to change it |
|---|---|---|
| Patch before feature release? | Yes, because sync and facet defects were reproduced | Proof the affected behavior is already fixed in a newer baseline |
| Shared sync fields? | Catalog fields with explicit policy; keep launch/install configuration device-local | A concrete portable-launch requirement and safe mapping model |
| Conflict resolution? | Preserve both versions and ask during review for incompatible edits | A demonstrated merge rule that cannot silently discard user intent |
| Automatic sync? | Defer; manual preview/apply first | Convergence/recovery tests plus successful multi-device use |
| Migration entry point? | Complete single-file support before multi-directory expansion | Real migration fixtures showing single-file UX is insufficient |
| Shelf session logging? | Defer new logging subsystem; finish catalog/launch distinction | Specific user need and a clear analytics/session contract |
| Default-on SQLite? | Keep opt-in | Semantic parity, repeatable speedup, acceptable rebuild/failure behavior |
| New discovery features? | Defer | User feedback stronger than the confirmed workflow gaps |
| Flathub launch date? | Separate from 1.10.0 scope | Maintainer approval and completed external review |

## 14. Definition of a successful update

The update is ready when a user can migrate a supported library with an accurate preview, distinguish shelf entries from broken installations, launch once reliably from desktop or couch mode, and use any included sync workflow without losing local configuration or edits. Optional acceleration must preserve the promised API behavior. Translations and assets must work from installed packages. Tests, documentation, and release claims must agree.

Success should be demonstrated through local test evidence and opt-in feedback, consistent with OpenBox's no-telemetry stance. Counts of new modules, routes, dashboards, or features are not release success criteria.

## 15. Primary repository references

- `AGENTS.md`: architecture, dependency, token, test, and PR requirements.
- `docs/CHANGELOG.md`, `docs/RELEASE_NOTES.md`, `docs/PARITY.md`: current advertised capabilities.
- `docs/adr/0032-sqlite-read-model-graduation.md`: limited read-model role and JSON source of truth.
- `docs/adr/0033-launchbox-xml-migration.md`: current import scope.
- `docs/adr/0035-library-sync-mounted-folder.md`: current sync promise and known conflict ceiling.
- `docs/adr/0036-manual-shelf-entries.md`: intentionally minimal shelf scope.
- `cloud_sync.py`, `handlers/health.py`: sync implementation and transaction boundary.
- `pkg/parity/parity_launchbox_import.py`, `pkg/state/imports.py`: preview/apply decisions.
- `metadata.py`: metadata database ID use.
- `handlers/library.py`, `pkg/state/cache.py`: shelf creation and public projection.
- `pkg/state/sqlite_readmodel.py`, `pkg/parity/parity_filter_presets.py`: SQL/JSON facet behavior.
- `pkg/state/launch.py`, `docs/reliability.md`: launch lifecycle and documented scenarios.
- `scripts/check_tests.py`, `.github/workflows/ci.yml`: actual local/CI gate thresholds.
- `Makefile`, `build_appimage.sh`, `io.openbox.GameLauncher.yml`: packaging differences.
- `docs/development/PERF.md`, `scripts/perf_bench.py`: historical metrics and performance gates.
- `docs/flathub-checklist.md`: remaining distribution work and submission boundary.
