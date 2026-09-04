# Changelog

All notable changes to OpenBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.9.0] - Unreleased

### Mood Match — Adaptive Cover Theming
- New **Settings → Appearance** toggles "Adaptive cover theming" and "Adaptive theming in Big Box" (`mood_match_enabled`/`mood_match_bigbox`), off by default.
- `static/mood.js` extracts a live 5-color palette (primary, ink, secondary, glow, tint) from the selected/focused cover and applies it to the selected card, detail hero, play button hover, and Big Box background/cover ring via `--mood-*` CSS tokens.
- New tokens added to `static/app.css :root` and all five stock themes; palette extraction is a fast 4×4×4 RGB bin quantizer with a documented upgrade path (ADR 0026).

### Picker
- New **"What should I play?"** picker replaces the random "Surprise me" button. Pick by available time, mood (action/chill/story/retro/party), familiarity (new/favorite), and number of players.
- `static/picker.js` dialog renders the top pick with an explanatory reason, quick Launch / Details / Again actions, and a "Just surprise me" fallback.
- `POST /api/v2/library/pick` returns scored, scoped recommendations via `pkg/parity/parity_picker.py` and `handlers/picker.py` (ADR 0028).

### Constellation
- New **Tools → Constellation** opens a full-screen, pan/zoomable relationship graph of the library.
- Backend `GET /api/v2/library/constellation` returns capped, deterministic nodes/edges by series, developer, publisher, genre, platform family, and co-play history.
- `static/constellation.js` runs a client-side spring-electric layout on a canvas; clicking a node dispatches `app:show-game` to select it in the library.
- Edge colors are tokenized as `--constellation-edge-*` and defined in all five stock themes (ADR 0028).

### Wrapped + Timeline
- New **Insights → Wrapped** opens a printable "Your Year in Games" report with playtime, sessions, streaks, progress, and top game/platform/genre.
- `GET /api/v2/insights/wrapped?year=YYYY` returns privacy-safe annual aggregates.
- New **History → Timeline** tab shows sessions grouped by day with covers and recording badges.
- `GET /api/v2/history/timeline?days=90` returns grouped sessions; recording values are basenames only (ADR 0029).

### Mastery
- New **Tools → Mastery** opens a completionist dashboard with stacked per-platform and per-decade bars over local progress states (never/played/beaten/completed/mastered), plus a RetroAchievements column.
- `GET /api/v2/insights/mastery` returns platform/overall/decade aggregates read from the existing RA disk cache — zero new network calls (ADR 0030).
- `static/mastery.js` renders tokenized `--mastery-*` segments (defined in all five stock themes); clicking a segment filters the library to that platform.

## [1.8.0] - 2026-09-02

### Navigation & Routing
- **Keyboard and gamepad navigation** for the library grid and list view (arrows/Home/End/Page, `f` favorite, Escape clear, gamepad via configurable controller map with edge detection) (ADR 0021).
- **Hash routing**: refresh and shared links restore platform/playlist/preset/query/selection/sort via `#/key/value` hash fragments (ADR 0021).
- **Sortable list-view columns** with persisted direction (`list_sort`/`list_sort_dir` settings); screenshot lightbox with prev/next/zoom; cover skeleton loading shimmer.

### ScreenScraper Provider
- **Per-ROM-hash metadata and media scraping** with credentials in `~/.env`, 1 req/s throttle, 429/5xx backoff, 30-day disk cache, region-priority media selection, and an https-only URL guard (ADR 0022).
- Additive v2 routes: status/test/search/info/match (batch, cancellable)/apply (durable job, downloads outside the state lock).
- Deliberately not wired into the LaunchBox match-review pipeline (ADR).

### Custom Gamescope Presets
- **User-defined presets** (≤16, unique names, bounded ints) with per-game override; per-game `gamescope_preset` wins over global (completes ADR 0016).

### Library Export
- **JSON/CSV export** with `all`/`platform`/`playlist` scopes, shareable-by-construction field projection, media paths opt-in, collision-safe filenames, and newest-10 rotation (ADR 0023).
- Additive v2 routes: queue durable job, download with Content-Disposition, list exports.

### ARM64 & Flathub Prep
- **aarch64 AppImage** release artifacts alongside x86_64; architecture-aware self-update refuses non-matching-arch releases (ADR 0024, un-defers ADR 0013).
- CI matrices build/attest/publish both arches (aarch64 on `ubuntu-24.04-arm`).
- Flatpak manifest runtime bumped `org.gnome.Platform 46` → `49`; AppStream `<content_rating>`, `<developer>`, and `<screenshots>` added; `docs/flathub-checklist.md` (submission stays a maintainer decision).

### Play Insights (Library)
- Play Insights now renders in the library with 30/90/365-day ranges, lazy IntersectionObserver load, debounced reload, and top-games deep links.

### Fixed
- `gamescope_preset`/`mangohud_enabled`/`show_insights` settings persisted instead of silently dropped (M0 whitelist bug).
- Context-menu "add to playlist" (`addGamesToPlaylist`) and Big Box "Achievements" (`openAchievements`) no longer throw ReferenceErrors.
- Chosen UI language now survives reload via `openbox-locale` localStorage.
- `app:state-refreshed` event dispatched (debounced) from `library.js refresh()`.
- Changed-line coverage gate skips unmeasured test/script files (ADR 0025).

## [1.7.2] - 2026-09-01

### Localization

- Full **i18n system** with `data-i18n` attributes in `index.html`, `t(key)` in JS via `static/i18n.js`, and JSON locale files for **English, Spanish, German, French, and Portuguese** in `locales/`.
- Settings → Interface language selector populated from `available_locales` in `public_settings`; switching re-translates the UI without reload.
- `scripts/check_i18n.py` gate verifies 100% key coverage across all locale files; wired into `make check`.
- Removed the "Localization is planned for a future release" note; localization is now live.

### Scale Foundation

- Optional **SQLite read model** (`pkg/state/sqlite_readmodel.py`) behind `OPENBOX_ENABLE_SQLITE_READ=1` env flag. Provides FTS5 full-text search (with LIKE fallback), indexed filtered queries, and GROUP BY facets. Disabled by default; zero behavior change when off.
- `query_parity_check()` verifies SQLite results match the JSON read path.

### Deck Polish

- **Gamescope presets**: 8 profiles (Steam Deck, HD, 1080p, 1440p, 4K, integer, stretch, borderless) in `pkg/parity/parity_gamescope.py`; selectable in Settings → Controller.
- **MangoHud** performance overlay toggle; `apply_mangohud_env()` sets `MANGOHUD=1` on game launch when enabled.
- Controller bench tab in Settings with live gamepad SVG visualization.

### Emulator Health

- **BIOS SHA1 drift detection** in Launch Doctor: reports `BIOS_SHA1_DRIFT` when a BIOS file exists but its hash doesn't match the expected value in `emulator_defs/*.yaml`.
- Health badge CSS classes (ok/warn/fail) with tokens in `static/app.css` and all 5 themes.
- `GET /api/v2/emulators/registry?health=1` returns `bios_ok`/`firmware_ok`/`core_ok` per adapter.

### Smart Collections & Backup Diff

- **Visual chip builder** for filter presets: `rules_to_chips()` and `chips_to_rules()` in `pkg/parity/parity_filter_presets.py` convert between preset rules and UI chip descriptors.
- **Backup diff**: `GET /api/v2/backup/diff?archive=<name>` compares current library against a backup archive, returning added/removed/changed game IDs and settings change status.

### Gates & Release

- `scripts/check_i18n.py` wired into `scripts/check_tests.py` as a strict gate.
- New runtime module `pkg/state/sqlite_readmodel.py` added to `runtime_modules.txt` (115 entries).
- 6 new locale-serving GET routes + 1 backup diff v2 route.
- Coverage: 81% total (floor 70%), web_app 55% (floor 54%), sqlite_readmodel 90% (floor 85%).
- Token baseline: 0 (15 new tokens across gamepad + health, all in `:root` + 5 themes).
- ADRs: `docs/adr/0014-sqlite-read-model.md`, `docs/adr/0015-i18n-system.md`.

## [1.7.1] - 2026-08-29

### Play Insights

- Local-first **Play Insights** dashboard: 366-day heatmap (levels 0-4), current/longest streak, top platforms/genres, momentum (last 30 vs previous 30) from `history` + `games` — no new storage, no telemetry. `GET /api/v2/insights/summary` + `GET /api/v2/insights/heatmap?days=&end_date=`.

### Performance

- Virtual spacer-window library grid with `localStorage['openbox-virtual-grid']` kill-switch, `IntersectionObserver` + `contain-intrinsic-size`, rAF coalescing preserved.
- Trigram search off main thread via `static/worker.search.js` with main-thread fallback; identical results.
- `pkg/state/cache.py` `FacetCache` LRU (64) + budget + epoch bump on `_invalidate_all()`; `state_store.py` 50 ms micro-batch coalesce, single fsync.
- `scripts/perf_bench.py` now supports `--json-out` alias and artifact-friendly JSON; gates remain 10k/20k (20k <15 ms for insights heatmap).

### Setup & Launch Doctor Polish

- Setup preview `preview_document` now includes human `message` (“Found 342 games — 12 need your pick →”) for progress storytelling.
- Launch Doctor every blocking check now carries `fix_action {kind, label, payload}` (`flatpak_install`, `reveal_bios_path`, `pick_core`, `explain_token`) — actionable buttons instead of red badge only.

### Frontend & Themes

- New tokens in `static/app.css` `:root`: `--overlay-insight-cell-0`…`--overlay-insight-cell-4`, `--border-insight`, `--shadow-insight`, `--surface-insight-card`, `--focus-ring`; all 5 themes updated.
- Insights panel CSS: cards, heatmap grid 53×7, ranked lists, streak, legend; lazy-loaded via `static/insights.js`.

### Gates

- `runtime_modules.txt` + shim `parity_insights.py`, `handlers/insights.py` wired; `routes.py` 4 new GETs; `v1_contracts.json` unchanged (frozen).
- `make check` green: ruff, py_compile, `check_tokens` 0, `check_v1_contract` 60 routes, `check_runtime` 114, `check_frontend` eslint/tsc.

## [1.7.0] - 2026-08-26

### Library Setup Center

- Guided **Set up library** workflow with preview-before-commit imports, paginated review, emulator readiness, metadata enrichment, and completion actions.
- Side-effect-free scan previews with idempotent commit, stale-preview guards, and `import_batch_id` tagging for post-import filtering.

### Activity Center

- Durable operation service backed by `operations.json` with queued/running/cancelling/done/partial/error/cancelled/interrupted states.
- Persistent top-bar Activity control, SSE progress, cancellation, retry/resume, and legacy `/api/jobs` compatibility.

### Launch Doctor

- Preflight validation for game paths, adapters, Flatpak/native executables, BIOS/firmware, and tokenized launch arguments.
- Registry-driven emulator detection with explicit ambiguity handling and launch precedence rules.

### Additive v2 API

- Exact `/api/v2/*` routes for setup preview/commit, emulator registry, launch preflight, metadata match review, and durable jobs (see ADR 0010).
- Stable error codes for preview staleness, unresolved candidates, job conflicts, and cloud sync failures.
- Canonical `library.json` schema remains version **6**; previews and operations use separate disposable storage.

### Performance & scale

- Formal support target of **20,000** games with query-cache correctness, bounded index fallback, and performance gates.

### Packaging

- Release-gated **x86_64** artifacts: Ubuntu 22.04 **AppImage** and **Flatpak** (runtime 25.08).
- No Flathub store submission in this release; no telemetry added.

## [1.6.0] - 2026-08-23

### Architecture & State Management

- Decomposed `webapp_state.py` into focused state modules: `pkg/state/imports.py` (import orchestration, duplicate merging, auto-import), `pkg/state/commands.py` (command execution), and `pkg/state/registry.py` (process and session tracking with typed `Session` dataclass), keeping a lightweight backwards-compatible re-export facade.
- Centralized launch token expansions (`{path}`, `{name}`, `{dir}`, `{stem}`, `{platform}`, `{app_id}`, `{heroic_app_id}`, `{lutris_id}`, `{rom_name}`, `{DataDir}`, etc.) into `pkg/parity/launch_tokens.py`.
- Consolidated caches and locks into coordinated `CacheEpoch` dataclass with atomic full invalidation (`_invalidate_all()`).

### Frontend & Accessibility

- Replaced details/summary tools dropdown with fully accessible WAI-ARIA button and menu pattern (`#toolsButton`, `#toolMenu`, and `#toolsWrap.open`) supporting full keyboard navigation (Arrows, Home, End, Escape, Tab).
- Memoized search index with LRU cache, debounced input, and bounded trigram expansion for instant lookups across 20k+ games.
- Implemented dialog focus traps with inert fallbacks and proper focus restoration.
- Memoized grid geometry calculation for faster library view rendering.

### Security & Hardening

- Added `frame-ancestors` directive to Content-Security-Policy (CSP) headers.
- Hardened exception handling across handlers, state imports, commands, and SSE streams to eliminate broad except catches and add structured error logging.
- Added input validation and authentication checks to native dialog, window, and emulator scan endpoints.
- Added performance benchmark write-path gate (<500ms for 10k games).

## [1.5.1] - 2026-08-19

### Performance

- Optimized state writes with dirty-field tracking, batched snapshot persistence, and cached library projections for large libraries.
- Accelerated import scanning, metadata batching, and archive inspection throughput.
- Streamlined BigBox CoverFlow rendering and indexed title search matching.
- Improved native host startup responsiveness with non-blocking IPC polling.

### Changed

- Enhanced cross-store import consolidation across Steam, Heroic, Lutris, Faugus, and ROMs using canonical identity normalization.
- Hardened CLI help formatting and argument parsing for headless and native host invocations.
- Updated parity compatibility shims and LaunchBox feature matrix documentation.

### Fixed

- Ensured completed background job futures are released synchronously to eliminate future and memory retention.
- Hardened import endpoint error handling and input validation against malformed payload structures.

## [1.5.0] - 2026-08-18

### Added

- Edit Game modal now features Previous (← Prev) and Next (Next →) navigation buttons to rapidly cycle and edit games in the active filtered/sorted library without reopening the modal.
- Media cleanup can now be scoped to single platforms in addition to full library scans via `POST /api/media/cleanup`.
- Added "Reset play statistics" to the game right-click context menu and bulk edit wizard, clearing `play_count`, `playtime_seconds`, and `last_played`.
- Filter presets and smart filter playlists can now filter games by capability rules: `has_saves`, `has_achievements`, `has_missing_media`, and `has_highscores`.
- Added `Ctrl+Alt+Q` (and `Ctrl+Alt+R`) global shortcut to shuffle and focus a random game in the desktop grid and list views.
- Search and filter queries now support acronym matching for game titles (e.g., `oot` matches *The Legend of Zelda: Ocarina of Time*, `mgs` matches *Metal Gear Solid*, `sotn` matches *Castlevania: Symphony of the Night*).
- Expanded dynamic launch variables for custom emulators and launch commands, supporting `{ImagePath}`, `{dir}`, `{Dir}`, `{file}`, `{File}`, `{stem}`, `{FileNameWithoutExtension}`, `{Platform}`, `{EmulatorDir}`, and `{DataDir}`.
- Added Proton and Wine prefix manager (`/api/wine/*`), Faugus scan and import (`/api/faugus/*`), and Eden Switch emulator definition (`emulator_defs/eden.yaml`).
- Added canonical identity normalization across Steam, Heroic, Lutris, Faugus, and ROMs with duplicate game consolidation in library health checks.
- Added `--fullscreen-width`, `--fullscreen-height`, and `--resolution <WxH>` CLI flags to control kiosk and native window viewport sizing.

### Fixed

- Resolved frontend module reference errors, prevented media dialog DOM accumulation, fixed play queue and tag selected game lookups, and expanded BigBox menu controller navigation.
- Worker job queues and webapp state cleanup now guarantee cleanup on failure and process termination to prevent queue and process leaks.
- Preserved shared media files during media deletion via realpath reference counting.
- Stale Unix socket single-instance focus handling and native host boot cleanup now recover cleanly from crashed instances.

### Hardened

- Active sessions are now persisted and reconciled against PID, start time, and cmdline verification, marking abandoned sessions and reattaching watchers.
- Launch preparation is transactional with explicit 8-phase lease tracking.
- Backups and exports centralize credential and secret redaction with manifest tracking, preserving local secrets on restore.
- Performance benchmark runner covers full operational matrix with 10k game scaling gates.

## [1.4.0] - 2026-08-17

### Fixed

- AppImage and Flatpak now bundle every static module, fixing 404s for util.js, state.js, library.js and other chunks that left the top bar unresponsive. Routes now handle future JavaScript chunks without manual table updates. Fixes #19.
- AppImage bundling now creates parent directories for pkg/parity modules so the build does not fail on packaged layouts.
- Build provenance, SBOM generation and packaging tests now cover the full static set so the UI ships complete.

### Changed

- Repository layout reorganized: docs moved to docs/, tests to tests/ and parity modules to pkg/parity with backwards compatible shims at root. Agent conventions and layout docs added.
- CI, test runner, coverage gates and token hygiene updated to handle both flat and packaged layouts. Docs paths fixed after reorg.

## [1.3.0] - 2026-08-16

### Hardened

- Plugin execution now uses bubblewrap with isolated namespaces, no network access, and temporary mounts for home, temporary, runtime, and removable-media paths when the host supports it. If the sandbox cannot be created, enabled plugins are skipped by default; `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` is an explicit opt-in for trusted local plugins.
- Gamescope regression tests now launch in their own process groups and terminate the full group on timeout, preventing nested gamescope processes from surviving the test gate.

### Changed

- Cloud sync, save and backup restore, launch handling, settings validation, metadata application, Lutris import, 7z validation, webhook validation, filter matching, and game resolution were split into focused helpers, keeping the security-sensitive paths easier to audit and test.
- The background job manager now has dedicated coverage for retries, cancellation, queue and name limits, bounded results, shutdown, and completed-future cleanup.
- Dead backend code and obsolete browser, screenshot, migration, and performance-capture scripts were removed.
- From-source setup documentation now explains the tokenized UI URL and the supported `OPENBOX_ENV_FILE`, data-directory, home-directory, and user-config locations.
- CI and release tooling now use the refreshed GitHub Actions and JavaScript dependencies, with CodeQL action components kept on one version and grouped Dependabot updates for future changes.

## [1.2.0] - 2026-08-15

### Fixed

- SteamOS AppImages no longer export bundled libraries into the host shell, fixing startup failures where `/bin/bash` could not resolve `rl_print_keybinding`.
- Webhook delivery rejects non-public destinations, pins validated DNS results, disables proxies and redirects, and bounds response reads.
- Library and save backups use private atomic files, reject unsafe archive entries, and protect restore paths against symlinks and archive replacement races.
- Native bridge authorization now requires the exact OpenBox origin, including its dynamic port.
- Gameyfin validates IDs before path construction, contains filesystem operations under the install root, requires HTTPS, and verifies supplied checksums.
- 7z extraction rejects links, operates on a bounded snapshot, and validates the staging tree before promotion.
- Media and document reads enforce approved roots, including symlinked parent checks.
- Environment loading accepts only owner-controlled files and supported keys, and no longer searches the current directory.
- Job and SSE queues now have explicit capacity, expiry, cleanup, and slow-client behavior.

### Changed

- Release artifacts require Ed25519 signatures against the pinned production public key.
- Release jobs separate build, provenance attestation, and publication permissions and refuse asset overwrites.
- Build and CI inputs are pinned, the SBOM is generated from the completed AppImage, and Puppeteer 25.7.0 resolves the audited npm dependency issues.
- Plugin catalogs require a pinned digest, HTTPS package URLs, and package checksums.

## [1.1.0] - 2026-08-15

### Fixed

- Cloud sync: the local-wins merge branch contained a dead condition (`remote_played > local_played` can never be true there), so newer per-field remote values were silently dropped.
- State backup now mirrors the latest committed primary, staged atomically, instead of aliasing the brand-new write.
- Plugin environment filtering fixed a typo (`GAMEFYIN_` -> `GAMEYFIN_`), so correctly spelled Gameyfin variables are stripped from plugin subprocesses.
- Play queue: skip flags recorded while advancing are now written back to state before a valid item is returned, so they can no longer silently disappear.
- Queue `path_exists` now checks the filesystem instead of reporting any nonempty path string as existing.
- OBS status: `recording` now requires a recording file produced within the last two minutes instead of reporting any running OBS process as actively recording.
- Steam and Lutris imports now verify the Flatpak app is actually installed (`flatpak info`) before building a `flatpak run` command.
- IGDB: time-to-beat came from a nonexistent `time_to_beat` field on the games endpoint. It now queries the separate `game_time_to_beats` endpoint and converts its seconds value to hours.
- Gameyfin: catalog requests the provider list once; raw responses are returned open and closed by the caller; the tautological `str(folder) if installed else str(folder)` is gone.
- Job manager: completed futures are now released via a done callback so job bookkeeping cannot accumulate indefinitely.
- Webhook retry: the injected clock now measures the sleep duration and warns when the wall-clock sleep overshoots.
- The emulator-defs YAML fallback parser no longer decides a key is a list based on whether its name ends in "s"; indented values build sequences from the actual shape.
- Native dialog bridge: selected paths are now JSON-quoted strings, so paths with spaces or quotes produce valid JSON and no longer leak the `g_strescape` allocation.
- Ed25519 point decoding now rejects out-of-range coordinates, small-order points, and off-curve values in both `updates.py` and `scripts/verify_release.py`, and checks the canonical scalar before point arithmetic.
- `remove_exclusion` returns the number of removed entries; the API route reports `removed` truthfully.
- `sanitize_settings` no longer iterates a non-dict input into garbage dropped-key lists.
- The 7z archive validator counts a final member even when the listing omits the trailing blank separator.
- Screenshot capture: the previously ignored `window_hint` now selects active-window flags for gnome-screenshot, spectacle, and scrot.

### Changed

- `_tdp_args` returns only the argument list; the unused milliwatt value is gone.

## [1.0.1] - 2026-08-15

### Fixed

- Big Box hybrid mode: platform buttons were emitting a broken `data-bigbox-AppState.platform` attribute that the click handler never matched, so switching platforms did nothing. The attribute now matches the selector.
- IGDB search sent a malformed `&AppState.platform =` query parameter instead of `platform=`, dropping the platform hint from searches.
- Custom-field keys in the details pane were rendered without escaping; a crafted key could inject HTML. Keys and values are now both escaped.
- The session token stayed in the browser address bar after load. It is now scrubbed from history immediately, with deeplink parameters preserved.

### Changed

- CSP tightened: `script-src 'self'` without `'unsafe-inline'`, plus `object-src 'none'` and `base-uri 'none'`.
- Requests without a Host header are now rejected instead of bypassing the loopback check.
- The SSE stream now carries the same security headers as every other response.
- The startup URL printed to stdout no longer contains the session token.
- The updater verifies an Ed25519 signature when a release publishes one, and skips with a loud warning while the public key is still the placeholder.
- WebKit rendering defaults changed: dmabuf renderer disabled unless `OPENBOX_ENABLE_DMABUF` is set (fixes silent window failures on AMD GPUs, including Steam Deck), and hardware acceleration switched to on-demand.

### Hardened

- The native host now validates full URIs (scheme, host, no userinfo, no control characters) before handing anything to the default handler.
- Reveal-in-folder is restricted to paths under the data directory or home directory.
- The native bridge rejects suspicious payloads instead of evaluating them.
- Plugin catalog downloads now require a valid sha256 checksum; entries without one are refused.
- Plugin subprocess environments are scrubbed of token, password, secret, and API-key variables.
- `before_launch` plugin hooks can no longer swap the launch binary or move the working directory outside the game or data directories; tampered results fall back to the original command.

### Fixed (launch reliability)

- AppImage launches now route through the fallback ladder instead of exec-ing the native host directly, and all launch failures are written to `~/.local/share/openbox-game-launcher/openbox-launch.log` instead of vanishing on a double-click.
- The native host writes its own log and the single-instance message is no longer invisible.
- Game Mode with no kiosk browser installed now prints the server URL instead of failing silently.

### Verification

- `./run_all_tests.sh`: 47 test files, 0 failures.
- `make check`: lint, compile checks, coverage floors green.
- CI smoke test now covers Big Box platform switching and IGDB search parameters; JS linting runs in CI; Dependabot watches GitHub Actions and npm.

## [1.0.0] - 2026-08-14

### Added

#### Native window (native-first)

- `native_host.c` (C, WebKitGTK) renders the same `index.html`, `app.js`, and `app.css` as the web UI in a native window, with smooth scrolling, hardware acceleration, and the app's exact background color. The native host owns server lifecycle, single-instance locking, minimize-to-tray, window geometry persistence, and a fallback ladder (system-browser chrome-less app window, then the default browser) when WebKitGTK is missing or the host fails to start.
- `openbox` and `openbox-native` launchers resolve the share directory across repo, Makefile/Flatpak, and AppImage layouts; `openbox --web` remains the development opt-out to the loopback web UI in a browser.
- Native IPC: `/api/native/*` routes with dynamic capability reporting, plus a JS↔C bridge (`window.openboxNative`) for native dialogs, external opens, reveal-in-file-manager, and window chrome including Big Box fullscreen enter/exit.
- Server-Sent Events: `/api/events` streams session and job events; the job manager emits observer events and polling remains the fallback. SIGTERM/SIGINT teardown stops running sessions and drains work before exit.

#### State and API contract

- Schema v5 adds a host-owned `ui_state` block; existing games, settings, playlists, and history migrate untouched.
- The v1 API contract is frozen: `contracts.py` + `v1_contracts.json` (60 routes) with `scripts/check_v1_contract.py` wired into the coverage gate and CI, and `test_v1_aliases.py` pinning legacy aliases.

#### Batch metadata auto-match

- One "Auto-match library" action binds every unmatched game whose title exactly matches the LaunchBox Games Database, replacing the one-game-at-a-time dialog flow. Only exact normalized-title hits qualify, so ambiguous titles are left unmatched for manual confirmation.

#### Frontend

- Grid cover-grouping by aspect ratio (default), persisted as `cover_grouping` and toggleable from the library header.
- The topbar regroups into Library, Actions, and Tools zones; the empty-state import surface covers every storefront path.
- The dialog manager traps focus and closes on Escape.
- Scroll-lag fixes: rAF-coalesced grid rendering, backdrop blur removed from the base scroll path, hover-gated cover transitions, and a constrained workspace grid row.

#### Tests and documentation

- `test_native_ipc.py` and `test_sse.py` cover the native bridge and the event stream.
- The screenshot pipeline now waits for Big Box and game-detail views to settle and enriches fixture metadata (ESRB, max players, play time, region, play mode, wiki links).
- `docs/adr/0001-native-host.md` and `docs/native-host-contract.md` document the host/server split and the IPC contract.

### Changed

- The 260-method `Handler` class is split into capability mixins under `handlers/` (data, library, settings, media, metadata, imports, sessions, extensions, health, emulators, native). `web_app.py` drops from 3,755 to 1,628 lines and keeps server plumbing, shell/SSE serving, bootstrap, and lifecycle; each mixin's methods resolve the live `web_app` namespace at call time, so response bytes and route wiring are unchanged. Authentication is centralized in dispatch and route tables resolve module-qualified method names.
- The removed Tk interface no longer ships: AppImage dependencies drop python3-tk/tcl/tk in favor of WebKitGTK 4.1 and GTK dev headers, and the AppImage installs the `handlers/` package. CI compiles the native host (libwebkit2gtk-4.1-dev) on pull requests so C build breaks fail early, and release CI ships the compiled host and the logo asset.
- Packaging installs the runtime modules and `handlers/` (tracked in `runtime_modules.txt`), with SHARE resolution for Makefile/Flatpak layouts.

### Fixed

- The AppImage build now creates the `handlers/` directory so the handler split installs correctly.
- The `gen_sbom` usage example version, stale 0.9.0 release-date and localization claims, and SECURITY.md support rows (the 1.0.x row is pinned in tests).

### Verification

- Ran `./run_all_tests.sh`: 47 test files, 0 failures.
- Ran `make check`: lint, compile, tests, coverage floors (59.0% total vs 55.0% floor; 65.0% `web_app.py` vs 44.0% floor), and the v1 contract check all pass.
- UI smoke test (`scripts/ui_smoke.sh`) boots a real server, drives the grid, and asserts the Tools menu still opens under every stock theme with no page errors.

## [0.9.0] - 2026-08-12

### Added

#### LaunchBox media catalog and archive manuals

- Full LaunchBox Games Database media downloads beyond covers, backgrounds, and screenshots: box backs, box spines, 3D boxes, clear logos, fanart, banners, title screens, cart fronts, cart backs, discs, and advertisement flyers. The metadata dialog, bulk download dialog, media audit, artwork gallery, image groups, and the auto-import setting all accept the expanded type set.
- Manuals are not in the LaunchBox feed, so the manual option now pulls a PDF or text manual (`MANUAL_SUFFIXES = (".pdf", ".txt")`) out of the game's own archive. The finder ranks `manual.pdf` first, then shortest and most recent candidates, caps the candidate list at 8 to stay safe on pathological archives, copies the winner next to the game's media, and never blocks a sync on a bad archive. When nothing is found, the game records a `manual: no manual in this archive` note that the UI surfaces.
- Metadata search maps the app's own platform names to their LaunchBox spellings: `Game Boy` to `Nintendo Game Boy`, `PlayStation` to `Sony Playstation`, `GameCube` to `Nintendo GameCube`, `Xbox` to `Microsoft Xbox`, and 22 more (26 aliases total, including `PC` to `Windows` and `DOS` to `MS-DOS`). Exact-name searches now rank platform-correct results first.
- The LaunchBox Games Database dialog shows library coverage facts (matched games, match ratio, and per-field media counts) once the local database is ready.
- Batch auto-match: one "Auto-match library" action binds every unmatched game whose title exactly matches the database, instead of matching one game at a time. Only exact normalized-title hits qualify, so ambiguous or partial titles are left unmatched for manual confirmation rather than guessed onto the wrong record.

#### Desktop window

- The desktop UI opens in a chrome-less app window by default instead of a browser tab: a Chromium-family browser opens with `--app=`, Firefox falls back to a separate window, and with no compatible browser the default browser is used. The Settings panel controls this with an "Open the UI in" option, and `--app-window` / `--no-app-window` override it at launch.

#### Engineering gates

- `make check` runs ruff lint, compile checks over every runtime module and test, the full test suite under coverage, and coverage floors in one command. CI enforces it on push, pull requests, and a weekly schedule. At the time, floors in `scripts/check_tests.py` were 60% total and 48% for `web_app.py` (now ratcheted higher; see `scripts/check_tests.py`), and the floors fail the build when coverage regresses.
- A version-sync check (`scripts/check_version_sync.py`) fails when `updates.py` disagrees with the README badge, metainfo, PARITY.md, the bug report template, or CHANGELOG, so a release can no longer ship with a stale version claim.
- Dev-only tooling (ruff, coverage) is pinned in `pyproject.toml` and lives in `.venv-dev`; the runtime app still has zero third-party dependencies.

#### HTTP layer

- The two monolithic dispatch chains (a 613-line GET and a 195-line POST if-chain) became a route registry in `routes.py`: 109 GET and 126 POST entries, each a named `Handler` method or dotted handler spec, with zero behavior change during the mechanical extraction. The contract-frozen v1 surface is 60 routes (`v1_contracts.json`).
- Structured errors in `api_errors.py`: every failure carries a stable machine code (`BAD_REQUEST`, `GAME_NOT_FOUND`, `MEDIA_NOT_FOUND`, `ROUTE_NOT_FOUND`, `MEDIA_JOB_RUNNING`, `STATE_UNAVAILABLE`, ...) and a per-request id that appears in the UI and the diagnostic log. POST handlers re-raise `ApiError` unchanged and convert legacy `ValueError`s into `400 BAD_REQUEST` instead of leaking them to the generic 500 path.
- A versioned `/api/v1` surface aliases the stable routes; legacy paths keep working for older clients.
- The library payload is gzip-compressed once per state change and served with a `private, no-cache` ETag, so polls get 304s when nothing changed: 5,000 games serve in about 2 ms at 638 KB instead of 13.8 MB (measured in `docs/development/PERF.md`).
- Settings saves now drop unknown keys against the settings key registry (`settings_schema.py`) with a diagnostic warning, instead of persisting junk from a stale client.

#### Reliability

- Rolling state snapshots: the state store keeps the last 5 committed states as timestamped JSON copies under `library.json.snapshots/`, rotated on every commit. `/api/state/recover` gained `dry_run` (preview what recovery would do) and `snapshot` (restore a named point in time) modes; snapshot names are strictly validated.
- Background jobs keep a bounded history of the last 50 finished jobs, exposed at `/api/jobs` and rendered in the Library Audit dialog as a jobs panel.
- Graceful shutdown: Ctrl-C / SIGTERM now stops running sessions and drains webhooks in the `finally` block instead of stranding game processes.
- The browser's session poll runs every 1s while a game is running and every 10s when idle, cutting idle wakeups on handhelds.
- Log redaction now covers RetroAchievements key and `client_secret` shapes in addition to tokens, passwords, and authorization headers.
- A local-only diagnostic report packager (`/api/diagnostic`) bundles redacted logs, system facts, and the version for pasting into issues.

#### Frontend

- `index.html` shrank from 3,117 lines to a 485-line shell; the JS and CSS live in `static/app.js` and `static/app.css`, served over `/static/*` with ETag caching and a filename whitelist. The server injects the token into the asset URLs so script and stylesheet loads authenticate.
- All 30 browser state globals moved into one `AppState` object; browser-only preferences (library view, image group, badge visibility, sidebar sections) persist through `localStorage` with a versioned key.
- Server errors surface in a persistent, dismissible banner with a "Copy details" action that includes the request id, and `window.onunhandledrejection` routes internal errors there.
- The `api()` helper maps 45 stable routes to `/api/v1` through a single `API_V1` map, so a future v2 rename is a one-object change.
- Accessibility: every dialog is `aria-modal`, toasts and lifecycle messages are live regions, the label floor rose from 11px to 12px, and the library heading leads the workspace.
- The interface language selector now ships English-only with an honest note; the five partial translations were removed until real localization lands.
- `scripts/ui_smoke.sh` boots a real server and drives the grid with puppeteer, asserting the grid renders, selection works, and no page errors fire.

#### Release and supply chain

- `scripts/gen_sbom.py` produces a CycloneDX 1.4 SBOM of the runtime modules, bundled data files, and the bundled Python stdlib.
- `scripts/sign_release.py` (Ed25519, maintainer-only) and `scripts/verify_release.py` (pure-stdlib RFC 8032 verification) sign and check release artifacts; `test_release_signing.py` proves the round trip and tamper detection.
- `scripts/release.sh` chains version sync, the make check gate, AppImage build, SBOM, checksum, optional signing, and a release notes draft, stopping before the human publish step.
- `scripts/gen_api_docs.py` generates the API v1 contract page from the live route tables.

#### Documentation

- `docs/reliability.md`: a 23-scenario edge case catalog (truncated state, full disk, orphaned sessions, wrong credentials, huge archives, broken symlinks, 20k-game libraries) with expected behavior and verification status per row.
- `SUPPORT.md`: the platform and runtime matrix plus reporting guidance.
- `TRIAGE.md`: response targets and the triage flow.

### Fixed

- Duplicate media cleanup now scans every media field, not just covers and backgrounds.
- Latent undefined names in `web_app.py` that would have crashed their code paths on first use: `automation.DEFAULT_ATTEMPTS`/`DEFAULT_TIMEOUT` (module referenced instead of the imported names) and `contained_path`/`read_limited` (used but not imported from `backend_io`).
- A stale `Request` import in the media catalog downloader.
- Lint debt across the gate rule set: default-argument calls (`home=Path.home()`), loop-variable closures, unused variables, lambda assignments, missing `check=` on `subprocess.run`, shebangs, and import placement.

### Verification

- Ran `./run_all_tests.sh`: 45 test files, 0 failures.
- Ran `make check`: lint, compile, tests, and coverage gates all pass (56% total, 44% `web_app.py`, against the then-current floors).
- UI smoke test (`scripts/ui_smoke.sh`) boots a real server and drives the grid with no page errors.
- `python3 scripts/check_version_sync.py` passes at 0.9.0.
- Perf bench at 5,000 games: gzip library 1.9ms / 638KB vs 13.7ms / 13.8MB plain (see `docs/development/PERF.md`).

## [0.8.2] - 2026-08-12

### Added

- Added Web UI surfaces for the persistent play queue, normalized game tags, Notification Center, and signed webhook settings.
- Added `/api/queue`, `/api/tags`, `/api/notifications`, and `/api/webhooks` contracts with bounded state, secret redaction, and destination validation.

### Fixed

- Box art now keeps its natural aspect ratio in the library grid, Big Box Stage, and CoverFlow views instead of being force-cropped into a single ratio, so games with non-standard artwork (for example SNES titles) display uncropped. Title-only covers keep the standard portrait box.

### Verification

- Ran `./run_all_tests.sh`: 39 test files, 0 failures.

## [0.8.1] - 2026-08-09

### Fixed

- Launching a game with no launch command and no matching platform profile now fails with a clear error before anything runs, instead of silently spawning a process that exits on the spot and reporting a normal session end. Games whose file is not executable get the same message.
- The web UI now shows the real outcome of a failed session. An immediate exit with a non-zero code reports "Session failed" with the exit code and a hint to check the launch command and emulator install, instead of the generic "Play time and history were saved" toast.
- Emulator installs no longer re-add the Flathub remote when it already exists, which previously made `flatpak remote-add --user` exit non-zero and abort the install. When a remote add or install does fail, the error now includes flatpak's own message instead of a bare non-zero status.

### Verification

- Added regression coverage for the emulator install path and ran the full suite.
- Ran `./run_all_tests.sh`: 36 test files, 0 failures.

## [0.8.0] - 2026-08-07

### Added

- Handheld performance profiles: per launch-profile TDP limits applied via `ryzenadj` at launch, with an optional restore limit when the session ends, gated by an `Apply handheld performance limits` setting (auto / always / off). `auto` applies only on Steam Deck / Bazzite game mode and battery-powered handhelds; a missing `ryzenadj` or permission error logs a warning and never blocks a launch.

## [0.7.0] - 2026-08-02

### Fixed

- Backup restore now merges archived settings back into `library.json` instead of a sidecar file the app never reads, so settings survive a restore.
- Restoring an older backup over a newer library is refused unless explicitly forced, and restore re-validates symlink parents after `mkdir` to close a zip-slip window.
- Save backups resolve relative `save_paths` against the game instead of the process working directory, and reject symlinked backup directories.
- Cloud sync propagates local deletions and resolves per-game conflicts by `last_played` instead of a stale global timestamp, so newer progress, ratings, and favorites win.
- Big Box exit no longer runs the app-exit `shutdown_commands`; only entering Big Box runs its configured commands.
- The media audit endpoint tolerates null media fields instead of returning a 500.
- Plugin updates are atomic with rollback on failure, and reinstalling a removed plugin no longer comes back disabled.
- The plugin `library` hook is TTL-cached so `/api/library` no longer blocks up to 5 seconds per plugin per request.
- Storefront catalog launch commands substitute their real identifiers (`{lutris_id}`, Steam, and Heroic).
- EmuMovies media searches URL-encode their query parameters.
- Bezel downloads extract into staging and swap atomically, so a corrupt archive no longer destroys the working bezel set.
- Emulator scan folders with `auto_update` enabled are re-scanned by the auto-import worker.
- `read_limited` rejects negative or huge `Content-Length` headers up front.
- Replaced backend jobs clean up their futures so the job manager does not grow unbounded.
- The state-store backup is written from the temp file before `os.replace`, so a crash cannot pair a fresh primary with a stale backup.
- Pre-release and build-suffixed release tags parse cleanly and are never treated as available updates.
- `.env` files strip inline comments while preserving `#` inside unquoted values.
- `openbox://` deeplinks reject foreign hosts and fail clearly when no server port is known instead of silently hitting a dead port.
- Folder-based session tracking matches path boundaries so `MyGame2` no longer matches `MyGame`.
- The native UI launches games detached from the launcher's process session.
- `rom_quality_score` no longer crashes on a missing or unreadable ROM.
- Duplicate-media cleanup prefers keeping the copy inside the allowed media roots.
- The metadata database temp zip is cleaned up when the download fails.
- User edits to stock themes are preserved instead of being reverted on every Themes dialog open.
- Gamescope guest detection also recognizes `STEAM_GAMESCOPE_RESTRICTED` sessions.
- IGDB token failures and Gameyfin provider fallbacks now surface readable errors.
- Flatpak packages include the diagnostic logging module.

### Changed

- The test runner now continues past failures and reports a per-file pass/fail summary instead of stopping at the first error.
- Tests no longer leak environment variables (`GITHUB_TOKEN`, `RA_*`, `OPENBOX_DATA_DIR`) into the shell or a real home directory.
- Documented `OPENBOX_DATA_DIR` and the RetroAchievements and EmuMovies environment variables in the README and `.env.example`.
- Refreshed user-facing documentation, package metadata, and installation guidance.

### Verification

- Added regression tests for every fix in this release.
- Ran `./run_all_tests.sh`: 32 test files, 0 failures.

## [0.6.0] - 2026-07-30

### Added

- LaunchBox-style advanced search in the Web UI, including field terms, quoted values, status filters, and negative terms.
- Ordered manual playlists with parent grouping, notes, membership editing, and keyboard-friendly reorder controls, alongside existing filter playlists.
- Game context actions, Ctrl/Shift multi-selection, configurable status badges, richer platform/category/playlist detail panes, related-game reasons, artwork galleries, and per-game launch profile overrides.
- Backup archive listing, manifest summaries, restore actions, expanded artwork groups, and metadata fields for controller support, disc count, portable games, and broken entries.

### Changed

- State persistence now uses schema migrations, stable game identities with legacy aliases, last-known-good recovery, process-safe transactions, atomic writes, and corruption preservation.
- Long-running backend jobs now expose bounded state, retries, cancellation, durations, and replacement protection. Request bodies, downloads, archives, media responses, save restores, backup restores, and plugin execution use bounded and validated paths.
- Packaging uses the runtime module manifest and stricter import and artifact checks. Flatpak support documentation and release validation were refreshed.

### Verification

- Added focused API and launch-profile regression tests.
- Ran `./run_all_tests.sh`, Python compilation, JavaScript syntax checks, diff validation, and a Chromium smoke test covering the new search, selection, context, playlist, backup, settings, media, and detail workflows.

## [0.5.0] - 2026-07-30

### Added

- Steam Game Mode guest support for Steam Deck, Bazzite, and similar gamescope sessions. Big Box opens fullscreen while Steam retains Input, Quick Access Menu, and TDP controls.
- Settings can remove all Steam-imported library entries at once. This keeps game files and media on disk.
- Local rotating diagnostic logs with a Settings copy button. Request and auto-import failures include timestamps, uncaught crashes include stack traces, and tokens and passwords are redacted.

### Fixed

- Session playtime and Gameyfin installs now use stable library identities after deletes or reorders. Gameyfin downloads stage safely and stream to disk.
- Storefront and update failures now return readable errors instead of dropped connections or uncaught errors.
- AppImage desktop launches now work through Gear Lever and desktop menus, and the AppImage instructions show the correct `--native` command for the Tk interface.
- Artwork downloads now use the active LaunchBox CDN instead of the retired image URL that returns a redirect loop.

## [0.4.10] - 2026-07-30

### Fixed

- AppImage desktop launches via Gear Lever and similar integrators: stop leaking bundled `LD_LIBRARY_PATH` into host `xdg-open`/browsers, open the UI with a clean-env `xdg-open`, use a unique `io.openbox.GameLauncher` desktop/icon id, and let `openbox://` start fall through to a normal server boot when no instance is running

### In case you missed it

If you jumped from an older build and skipped the last two releases:

- **0.4.8 — Steam Game Mode guest:** `--game-mode` opens Big Box fullscreen under gamescope on Steam Deck, Bazzite, and similar handheld images. Guest sessions are detected automatically, Big Box opens without a manual deeplink, and Steam keeps Input, QAM, and TDP while OpenBox tags its UI and non-Steam launches for Steam's overlay path.
- **0.4.9 — library reliability:** session playtime and Gameyfin installs update the correct library entry after deletes or reorders, Gameyfin downloads stage then replace and stream to disk, Lutris CLI failures return JSON errors, and empty release checksum files fail cleanly.

## [0.4.9] - 2026-07-30

### Fixed

- Session completion now credits playtime and history by stable game identity, so deleting another library entry while a game is running no longer updates the wrong title
- Gameyfin installs resolve library updates by Gameyfin ID instead of a stale array index, keep existing installs until a download succeeds, and stream downloads to disk instead of buffering whole files in memory
- Gameyfin provider lists ignore malformed non-object entries; empty passwords on connection tests no longer overwrite a stored credential for the probe
- Lutris import and storefront catalog routes return JSON errors when the Lutris CLI fails or times out, instead of dropping the HTTP connection
- Empty release checksum files raise a clear update error instead of an IndexError

## [0.4.8] - 2026-07-30

### Added

- Steam Game Mode guest support: `--game-mode` opens Big Box fullscreen under gamescope on Steam Deck, Bazzite, and similar handheld images, detects guest sessions, and best-effort sets `STEAM_GAME` window props on the OpenBox UI and non-Steam launches while leaving Steam Input, QAM, and TDP controls with Steam
- `parity_gamescope.py` module with portable gamescope detection, kiosk browser launching, and window tagging helpers
- Deck/Bazzite nested gamescope emulation harness (`scripts/emulate_deck_gamemode.sh`, `test_gamescope_deck_emu.py`) for verifying guest behavior on desktop hosts

### Changed

- Big Box mode now opens automatically when running under a gamescope session, no manual deeplink needed

## [0.4.7] - 2026-07-28

### Fixed

- AppImage desktop integration now starts the bundled application through `AppRun`
- GitHub update checks, plugin catalog downloads, AppImage zsync metadata, and repository links now use the canonical `vindeckyy/OpenBoxGL` location after the repository rename

## [0.4.6] - 2026-07-26

### Fixed

- Authenticated POST API requests now reject non-object JSON and request-shape type errors with JSON `400` responses instead of dropping the connection

### Changed

- Public identity, support, trademark, and notice documentation consistently distinguishes OpenBox Game Launcher from the Openbox window manager
- The repository default branch, CI triggers, issue links, and contributor documentation now use `master`

### Tests

- Added real-HTTP API boundary regressions for authorization, validation, exception mapping, partial settings updates, secret sanitization, and lifecycle errors

## [0.4.5] - 2026-07-24

### Added

- Filter presets, explorer facets, Big Box quick-switch presets, and import exclusions
- `openbox://` deep links, keyboard launcher support, granular library backups, and restore rotation
- Process tracking modes, optional IGDB metadata search, and YAML emulator definition packs

### Fixed

- Session restart handling, launcher menu formatting, ROM paths containing spaces, malformed tracking values, and unsafe backup archive paths
- Responsive and keyboard-accessible library controls, lazy media loading, and reduced-motion behavior

## [0.4.4] - 2026-07-24

### Fixed

- Settings persistence on partial saves, storefront-only POSTs, JSON error responses on catalog routes, Gameyfin install recovery, premium route auth, and session polling guards

## [0.4.3] - 2026-07-24

### Fixed

- Settings save merges omitted keys from existing state. Partial storefront POSTs no longer wipe watch folders or storefront auto-import flags.
- Storefront save posts only storefront fields instead of spreading empty Settings form values
- GET `/api/storefront/catalog` and `/api/gameyfin/providers` return JSON errors instead of dropping the connection
- Gameyfin install worker reports error state on invalid `library_id`; UI times out after ~60s instead of polling forever
- Premium GET routes require authorization; `/api/update` catches malformed release JSON
- Session poll no longer overlaps `refresh()`; `openReader` guards missing documents

## [0.4.2] - 2026-07-24

### Fixed

- Gameyfin installs run in a background worker; the UI polls `/api/gameyfin/install/status` instead of blocking the server
- Storefront Gameyfin settings use dedicated `storefront*` form fields; saving Settings no longer touches Gameyfin credentials

## [0.4.1] - 2026-07-24

### Fixed

- Update check no longer fails when a release omits a separate `.sha256` file; GitHub asset digests are used instead
- Settings update check returns readable errors instead of a browser network failure
- Top bar and library header scale cleanly at any browser zoom level

## [0.4.0] - 2026-07-24

### Added

- Gameyfin storefront integration: browse owned library, import, install/uninstall on demand in desktop and Big Box
- Library and Big Box filters for installed-only vs all owned games
- Ludusavi and Hoard save-tool hooks from the game detail pane (when installed on PATH)

## [0.3.0] - 2026-07-24

### Added

- LaunchBox Premium-equivalent features without a subscription: custom fields, ESRB metadata and filters, list view, platform categories, and bulk edit wizard
- Drag-and-drop import zone with multi-emulator install chooser and ROM version ranking
- Steam trailer and GOG media auto-download, RetroAchievements 7z scanning, and richer achievement stats
- Big Box hybrid scoped search, attract mode, startup video, bundled media packs, and controller prompt packs
- Localization for English, Spanish, German, French, and Portuguese
- Xbox 360, loose arcade, and Vita3K title resolution import helpers
- `parity_premium.py` module and premium API routes

### Changed

- Expanded PARITY.md to mark premium workflows as done and free
- AppImage, Makefile, Flatpak manifest, and packaging tests include `parity_premium.py`

## [0.2.0] - 2026-07-24

### Added

- Storefront Manager with catalog browse, uninstalled import, and startup auto-import
- Game Discovery Center with curated local discovery lists
- Big Box Stage, Hybrid, and CoverFlow layouts with gamepad mapping, pause overlay, and screensaver
- Parity modules for import, media hygiene, saves, integrations, and storefront workflows
- ScummVM, RPCS3, and Vita3K dedicated import endpoints
- RetroAchievements Big Box filters, pause access, and emulator launch injection
- EmuMovies and Bezel Project integration hooks
- OBS recording auto-attach on session close
- MAME community high score export and import
- Platform documents pane and sidebar section management
- Searchable settings, welcome wizard, and library health audit
- Expanded API and parity integration test coverage

### Changed

- Expanded LaunchBox parity matrix with Linux equivalents for premium workflows
- AppImage and Makefile packaging updated for new modules

## [0.1.0] - 2026-07-23

### Added

- Initial public release
- Web UI and native Tk UI
- Steam, Heroic, and Lutris import
- LaunchBox Games Database sync and media scraping
- Emulator profiles with Flathub auto-install
- RetroAchievements integration
- Save discovery and versioned backups
- Session tracking and plugin hooks
- AppImage, Flatpak manifest, and Makefile install targets

[1.7.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.5.1...v1.6.0
[1.5.1]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/vindeckyy/OpenBoxGL/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v1.0.0
[0.9.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.9.0
[0.8.2]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.8.2
[0.8.1]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.8.1
[0.8.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.8.0
[0.7.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.7.0
[0.6.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.6.0
[0.5.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.5.0
[0.4.10]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.10
[0.4.9]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.9
[0.4.8]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.8
[0.4.7]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.7
[0.4.6]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.6
[0.4.5]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.5
[0.4.4]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.4
[0.4.3]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.3
[0.4.2]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.2
[0.4.1]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.1
[0.4.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.0
[0.3.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.3.0
[0.2.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.2.0
[0.1.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.1.0
