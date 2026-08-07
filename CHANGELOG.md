# Changelog

All notable changes to OpenBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[Unreleased]: https://github.com/vindeckyy/OpenBoxGL/compare/v0.8.0...HEAD
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
