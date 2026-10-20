# OpenBox release notes

OpenBox 1.18.0 is a large release, and most of it is about what happens after the games are already in the library: fixing a platform name once instead of editing every game, moving artwork with the games, keeping a long download queue alive across restarts, restoring only the saves you choose, and getting more useful work out of plugins and emulators. The first-run screens were also rewritten so the next person who opens OpenBox has a clearer path from "no games" to "playing". Upgrade from any 1.17.x release. The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.17.0...v1.18.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.17.0...v1.18.0).

## The first run explains itself

- **One Add games menu.** Adding a single game, scanning a folder, adding a shelf entry, opening guided setup, and importing from a store now live under one top-bar menu instead of four separate buttons. The old buttons moved into that menu; nothing was removed.
- **Setup Center is shorter by default.** The guided path is six steps, with the longer, optional steps behind an expandable "advanced" section. The goal is to get a first game into the library before asking about the rest of the setup.
- **Help is one key away.** Press **F1** or **?** anywhere outside a text field to open Help & shortcuts. It includes a glossary of OpenBox terms and the actual keyboard and controller shortcuts, not a generic key list.
- **The empty library is one card.** It offers three clear starts: **Scan a folder**, **Connect a game store**, and **Add one game**. The previous row of six buttons is gone.
- **The details pane teaches by doing.** A new three-step checklist marks what is done and what is left. The library also gets a **Jump back in** card for the most recently played game, with **Play** and **View details**, so the first screen has a way back into a session.
- **Themes are visible before you apply them.** The Themes dialog now shows a swatch gallery for each theme. The default theme now uses an orange action colour with a teal focus colour, separating the thing you can click from the thing the keyboard is focused on.
- **Play insights moved below the grid.** The panels no longer push the games off the first screen.
- **The Tools menu is grouped and renamed.** Entries are grouped under Start here, Library, Imports & extras, and More. The details-pane Launch Doctor card now reads **Launch check**, and the Tools entries Netplay, Museum, and Library Audit now read **Online play**, **Kiosk mode**, and **Library checkup**.

## Fix the library, not every game

- **Rename or delete a platform in one place.** The platform view can rename a platform across its games, launch profiles, filter playlists, platform settings, and platform media. Delete asks you to type the platform name, then moves its games to Trash. Before this, changing "SNES" to "Super Nintendo" meant editing every game and every saved filter separately.
- **Move the artwork with the games.** A bulk platform change can move each selected game's cover, screenshots, videos, and other media into the new platform folder. If one move fails, the field change is kept and the error is listed so the move can be retried; the library does not silently point at a file that never arrived.
- **A download queue that survives a restart.** Large imports can queue artwork and media, and the queue is saved to disk. Closing OpenBox does not lose it. From the queue you can pause, resume, cancel, retry, and see per-item errors instead of one generic failure.
- **Automatic imports feed the queue.** When an import recognizes a game, its poster, square art, video, music, manual, and other media can be queued automatically. Global and per-platform limits keep one large platform from consuming every download slot.
- **EmuMovies joins the media sources.** It can fill covers, box backs, spines, 3D boxes, screenshots, title screens, marquees, cart fronts and backs, discs, clear logos, theme and snap videos, manuals, and music. Existing files are kept unless overwrite is requested.
- **Choose the order of metadata sources.** You can place built-in and plugin providers in a preferred order. Every search result shows which source it came from, and a bulk download can prefer plugin results. Partial-word searches now favour exact title matches and filter by platform.
- **LaunchBox downloads are checked before they reach the library.** A file is written only after the HTTP status, content type, and payload have been validated. Local database imports keep retry and progress reporting.
- **Library checkup tells you where a problem came from.** A new Source column, per-source counts, and a source filter separate a missing file from a bad emulator setting or a plugin problem. Plugin sources use their display names.
- **Filters can exclude as well as include.** Platform and category filters cycle through include, exclude, and clear, and the Source filter has an **Exclude** toggle. Presets and playlists can also match or exclude source, achievement source, region, text operators, and specific platforms. Exclusions survive a refresh.
- **Bulk Edit does literal find and replace.** You can preview a replacement in one field across the selected games, then apply it. Bulk Edit can also move those games' media during a platform change.
- **Progress labels are configurable.** The game editor and progress automation only offer the labels you enabled in Settings; a label that is turned off is rejected instead of being written.
- **Settings are easier to find and change.** Options search scans every category and focuses the matching field. The sidebar can be reordered and the Explorer section hidden. There is a font-family picker, editable custom-field dropdown options, platform-document ordering, a Report a problem link, a save-management toggle, a startup-error surface, and Big Box shutdown-on-exit. A shared placeholder cover replaces repeated missing-art text, and media cache cleanup removes stale generated files.

## Imports, plugins, and achievements

- **Steam tells you when a game is gone.** Imported Steam games now record whether their install folder still exists. A missing game gets a Missing badge and an Install handoff; an installed game gets **Verify Steam files**, which opens Steam's own validation for that app.
- **Optional service keys are stored privately.** Keys for IGDB, ScreenScraper, SteamGridDB, Gameyfin, and RetroAchievements are kept out of the public settings view and shown masked. They are no longer part of the settings JSON a browser can request.
- **Storefront scans show their work.** A scan reports progress per source, skips sources with no library games, stops after 30 seconds, and keeps whatever it found if it runs out of time. Per-source auto-import can set the target platform, include owned-but-uninstalled games, and limit the import to games that are not already in the library.
- **More places to import from.** PICO-8, MSU-1, and WHDLoad join the source list. RPCS3 imports external folders and ISOs from its `games.yml`.
- **Plugins can ask for specific access instead of a copy of the library.** A plugin declares what it provides and what it needs. OpenBox can answer read-only requests for the library, achievements, profiles, activity, leaderboards, catalog, metadata, and media, and gives each hook a bounded data bag, settings, and credential store.
- **Plugins can act like built-in sources.** A plugin storefront uses the normal catalog and import screens; plugin stats merge into the library; plugin discovery lists appear in the Discovery Center; plugin notifications use translated messages. The catalog browser shows each entry's author and homepage. Metadata results remember their source so bulk media can prefer them.
- **Plugins are safer to remove and reinstall.** Reinstalling from the catalog keeps settings, stats, and trust. Disabling or removing a plugin deletes its imported games, stops its cached stats from merging, and clears its settings, trust, permissions, data, credentials, and stats cache. Plugin settings have their own page under Settings > Plugins, and palette commands never run for a disabled plugin.
- **Discovery has a games-with-achievements shelf.** It recognizes totals from each achievement source, so a game with Steam or plugin achievements can be found without opening each game.
- **Steam achievements and playtime import with sane updates.** Totals only go up, so importing again cannot lower playtime or last-played dates. Achievement tabs show provider totals and source filters, manual overrides persist, and Steam and plugin sources refresh independently of RetroAchievements.
- **Automatic RetroAchievements matching covers more formats.** It now reads WiiWare WADs, M3U playlists, PlayStation CHD, and GameCube/Wii RVZ through `chdman` or `dolphin-tool`.
- **MAME high scores are readable in OpenBox.** Files in hi2txt's JSON format or its pipe-delimited table can be imported, exported, and shown in a searchable table. The Big Box running-game panel can show, export, and import them too.
- **The public RetroArch netplay lobby is available from OpenBox.** Open it from Tools or the Big Box menu and copy a room address without leaving the library.
- **Notifications and audit history catch up on their own.** The notification bell fetches its unread count at startup without opening the center. The launch audit keeps a compact rolling history in the cache folder for trend checks.

## Emulators, controllers, and Big Box

- **Manage Emulators is an editor now.** The dialog records a managed flag, per-platform overrides, and install/version status. A platform can override the emulator adapter, the RetroArch core, and extra launch arguments without changing the global choice.
- **MAME and RetroArch settings reach the launch command.** Display, fullscreen, effects, run-ahead, rewind, and upscaling can be written into launch arguments or a generated RetroArch configuration file. Update-all probes the installed versions, and an emulator can opt out of update checks.
- **MAME imports can be narrowed.** Filter by player count, skip CHD-dependent sets, and generate vertical playlists.
- **The main controls can be reassigned.** Settings > Controller records keyboard shortcuts for play, back, menu, pause, paging, search, settings, and the reader. Controller buttons can be cleared, and a duplicate assignment is swapped only after you confirm it. Controller types are shown as chips in the game editor and as badges on library cards.
- **File pickers start in the useful place.** Path pickers open at the current file's folder, or at the first game on the same platform, instead of at the last folder used for something unrelated.
- **Big Box accepts more input.** Pointer drag, mouse wheel, trackball, and spinner navigation work, with configurable sensitivity and wheel speed. Holding Back opens the menu. A battery indicator shows the charge reported by Linux on handhelds. Platforms, categories, and playlists can be hidden from Big Box.
- **Big Box video and sound are controllable.** Videos have controls and an audio mix. A startup video plays once, themes can add background music and a sound pack, and the details window remembers its position and size.
- **The reader remembers where you were.** Reader controls are remappable, and the reader keeps its theme and two-page spread. Image cells and Ctrl+I open a full-screen lightbox with labels and integer scaling.
- **Box art can be a 3D object.** Cassette and jewel-case models can use straight or angled backs and clear or solid cases, and each choice can be set per platform. The rotation lock can be free, horizontal, or vertical. The selected CoverFlow card uses the same model instead of a flat image.

## Saves and restore

- **Backups describe every file.** Each archived file now records its size, mode, modification time, and SHA-256. A version-history dialog lists older backups with add, overwrite, and unchanged counts.
- **Restore shows its work and restores only what you check.** The restore dialog lists the archive contents, supports a dry run, and restores the selected files after taking a safety backup. You no longer have to choose between "everything" and "nothing".
- **A game can remember a save-state slot.** The Save management card stores a RetroArch save-state slot for that game.
- **More save formats are recognized.** Automatic RetroAchievements matching can expand M3U playlists and hash PlayStation CHD and GameCube/Wii RVZ images through `chdman` or `dolphin-tool`.

## Fixed

- **Cross-device media moves on Windows.** Moving platform media to another drive now flushes the copied file through a writable handle and tolerates a filesystem that cannot `fsync` it, instead of failing with a bad file descriptor.
- **Manage Emulators no longer renders an empty version line.** It shows the probed version and an explicit note when update checks are off.
- **An older sidebar order no longer blocks a settings save.** Section ids from an earlier build are tolerated instead of rejecting the whole save.
- **Gameyfin auto-import follows the same exclusion and owned-uninstalled rules as the other stores.**
- **Old automatic-import settings migrate.** A legacy boolean `storefront_auto_import` entry becomes the per-source shape on the next settings save; readers keep accepting the old shape until then.

## Upgrading

`storefront_auto_import` values are now `{enabled, platform, include_uninstalled}` objects. The old boolean form is still read until the next save. No existing v1 API route was removed or changed.

## Known limits

- The new Tools, Add games, help, setup, onboarding, and Jump back in text is English in Spanish, German, French, and Portuguese for this release. Existing translations remain, and a translation pass will follow.
- Plugin service credentials and blank password-format plugin settings are excluded from backups.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
