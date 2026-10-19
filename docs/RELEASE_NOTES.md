# OpenBox release notes

OpenBox 1.18.0 makes the library easier to understand and much deeper to manage. The first run now has one Add games menu, a guided Setup Center, built-in help, and a Jump back in hero; behind it, platforms can be renamed or deleted with their media, media downloads queue durably, plugins gain host services, saves can be verified and selectively restored, and emulator, Big Box, and metadata controls go further. Upgrade from any 1.17.x release. The full list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.17.0...v1.18.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.17.0...v1.18.0).

## A clearer first run

The top bar now has one **Add games** menu instead of four separate buttons. It holds adding one game, scanning a folder, adding a shelf entry, opening guided setup, and importing from a store. **Setup Center** opens on a six-step quick path; advanced steps stay behind a toggle. Press **F1** or **?** for Help & shortcuts, including a glossary and the real keyboard/controller table.

An empty library is one illustrated card with Scan a folder, Connect a store, and Add one game. Once games are present, the details pane shows a three-step checklist and the library adds a **Jump back in** hero for the most recently played game, with Play and View details. The Themes dialog gains a swatch gallery, the default palette uses an orange action ramp with teal focus, and Play insights now follows the grid so games are visible on the first screen. Tools are grouped Start here / Library / Sources / More, and Launch Doctor, Netplay, Museum, and Library Audit now read as Launch check, Online play, Kiosk mode, and Library check.

## Platforms, media, and metadata

A platform view can **rename or delete** a platform across games, launch profiles, filter playlists, platform settings, and platform media. Deletion moves games to Trash and asks for the platform name first. Media moves are crash-safe: the field update is kept if a move fails, and `media_errors` reports what needs a retry.

**Media automation** adds a durable queue that survives restarts and supports pause, resume, cancel, retry, and per-item errors. Automatic imports queue poster, square, video, music, and manual downloads with global and per-platform limits; folder and storefront imports enqueue matched LaunchBox or provider games. EmuMovies joins the media providers, covering covers, box backs/spines/3D boxes, screenshots, title screens, marquees, cart fronts and backs, discs, clear logos, theme and snap videos, manuals, and music. The queue reports its items, pending queue, and per-state counts, and existing files are preserved unless overwrite is requested.

Metadata and media searches can mix built-in and plugin providers in a configured order; candidates carry their source, and bulk downloads can prioritize plugin results. LaunchBox media downloads verify status, content type, and payload before writing. Partial-word metadata search prefers exact matches and filters by platform, and local database imports keep retry and progress reporting. Library Audit gains a Source column, per-source counts, and a source filter; plugin sources use their display names.

Imported Steam games now record whether their install folder still exists. Missing games show a Missing badge and an Install handoff; installed games offer Verify files through `steam://validate/<id>`. Optional IGDB, ScreenScraper, SteamGridDB, Gameyfin, and RetroAchievements tokens stay outside the public settings projection and are shown masked.

## Plugins, sources, and achievements

Plugins can declare storefront, metadata, media, stats, emulator-settings, and discovery capabilities. Capability hooks get a bounded data bag, settings, and credential store, and may request read-only library, achievement, profile, activity, leaderboard, catalog, metadata, and media services. Plugin storefronts use the shared catalog/import UI; plugin stats merge monotonically; plugin discovery lists join the Discovery Center; plugin notifications resolve localized messages. The Discovery Center adds a "Games with achievements" shelf that recognizes per-provider achievement totals, and the catalog browser shows each entry's author and homepage. Metadata results persist their provider so bulk media can prioritize it.

Installed plugins can be reinstalled from their catalog entry without losing settings, stats, or trust. Disabling or removing a plugin deletes its imported games, stops cached stats from merging, and clears its settings, trust, permissions, data, credentials, and stats cache.

Storefront scanning now reports per-source progress, skips sources with no library games, stops after a 30-second budget, and preserves partial state. Per-source auto-import stores the target platform and can include owned-but-uninstalled games; imports can be limited to new games. PICO-8, MSU-1, and WHDLoad join the source list, and RPCS3 imports external folders and ISOs from `games.yml`.

Steam achievements and playtime import through the Steam Web API with monotonic merges. Achievement tabs show provider totals and source filters, manual overrides persist, and Steam and plugin sources refresh independently of RetroAchievements. Automatic matching also reads WiiWare WADs with the rcheevos TMD/content layout. MAME high scores parse hi2txt JSON or pipe-delimited tables, import/export, and render in a searchable table; the Big Box pause panel can show, export, and import them too. RetroArch's public HTTPS netplay lobby is available from Tools and Big Box.

## Emulators, Big Box, and saves

**Manage Emulators** gains an Edit Emulator dialog with a managed flag, per-platform overrides (emulator adapter, RetroArch core, and extra launch arguments), and install/version status. MAME and RetroArch settings cover display, fullscreen, effects, run-ahead, rewind, and upscaling, and render into launch arguments or `--appendconfig`. Update-all probes installed versions and honours a per-emulator opt-out. MAME imports can filter by player count, skip CHD-dependent sets, and generate vertical playlists.

Settings > Controller records keyboard shortcuts for play, back, menu, pause, paging, search, settings, and the reader; controller buttons can be cleared, and duplicate assignments are swapped only after confirmation. Settings define controller types shown as chips in the game editor and as badges on library cards. Path pickers open at the current file's folder or the first game on the same platform.

Big Box input now accepts pointer drag, mouse wheel, trackball, and spinner with configurable sensitivity and wheel speed; holding Back opens the menu, and a battery indicator reads Linux's sysfs battery. Platforms, categories, and playlists can be hidden in Big Box.

Big Box adds video controls and audio mix, a startup video that plays once, theme music and a sound pack, and a details window that remembers its position and size. Reader controls are remappable and persist theme and spread; image cells and Ctrl+I open a fullscreen lightbox with labels and integer scaling. 3D cassette and jewel-case models render box art, with straight/angled backs and clear/solid cases selectable per platform; the rotation lock can be free, horizontal, or vertical, and the selected CoverFlow card uses the same model.

Save backups now record each file's size, mode, mtime, and SHA-256. A version-history dialog lists older backups with add/overwrite/unchanged counts; restore lists archive contents, supports a dry run, and restores only checked files after a safety backup. A game can store a RetroArch save-state slot from its Save management card, and automatic RetroAchievements matching expands to M3U playlists, PlayStation CHD, and GameCube/Wii RVZ through `chdman` or `dolphin-tool`.

## Also in this release

- Filter presets and playlists can exclude values and match source, achievement source, region, text operators, and platforms. Filters cycle include → exclude → clear for platforms, categories, source, and smart collections, and exclusions survive a refresh.
- Bulk Edit can preview and apply a literal find/replace in one field, and can move selected games' media during a platform change.
- Progress labels are configurable; the game editor and automation only offer the enabled values.
- Settings can reorder the sidebar and hide Explorer. Options search scans every category and focuses the field; a font-family picker, custom-field dropdown options, platform-document ordering, report-issue link, save-management toggle, startup error surface, and Big Box shutdown-on-exit complete the settings surface. A shared placeholder cover replaces repeated missing-art text, and media cache cleanup removes stale generated files.
- Enabled plugin settings render under Settings > Plugins with the shared schema form; palette commands refresh per open and never run for a disabled plugin.
- Notifications fetch their unread count on boot without opening the center, and the launch audit appends a compact rolling history to `cache/launch_audit.jsonl` for trend checks.
- Tray menus on Linux and Windows launch the eight most recent games; `?deeplink=launch&id=<id>` starts a game directly.

## Fixed

- Cross-device platform media moves now flush the copied file through a writable handle and tolerate platforms that cannot `fsync`, so Windows migrations no longer fail with a bad file descriptor.
- Manage Emulators shows the probed version and an update-checks-off note instead of rendering nothing.
- Saving settings no longer rejects an older sidebar order that still contains retired section ids.
- Gameyfin auto-import honors excluded platforms and the include-owned-but-uninstalled option.
- Legacy boolean `storefront_auto_import` entries migrate to the per-source shape on the next settings save.

## Upgrade notes

`storefront_auto_import` values are now `{enabled, platform, include_uninstalled}` objects; readers accept the old boolean shape until the next save. No v1 route is removed or changed.

## Known limits

- The new Tools, Add games, help, setup, onboarding, and Jump back in copy is English in Spanish, German, French, and Portuguese for this release; existing translations remain, and a translation pass will follow.
- Plugin capability credentials and blank password-format plugin settings are excluded from backups.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
