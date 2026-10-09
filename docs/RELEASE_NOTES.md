# OpenBox release notes

OpenBox 1.16.1 answers the question "why won't this game start?" for your whole library, adds launch definitions for 18 more systems, and keeps the library live while it changes. It also carries the fixes from the 1.16.1 work: safer edits, probes that cannot hang, and dates with a time zone. Upgrade from any 1.16.x release. The fixes are not backported to older versions, and 1.15.x is no longer supported. The full list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.16.0...v1.16.1](https://github.com/vindeckyy/OpenBoxGL/compare/v1.16.0...v1.16.1).

## Know which games won't launch, before you press Play

Launch readiness now shows on the grid. A game the last launch check found blocked is marked **Won't launch**, and one with warnings is marked **Needs attention**. The marks come from the last check and disappear once your library has changed, so an old report never labels a game. Turn them off in Settings > Appearance if you don't want them.

The check keeps itself current. It runs with your scheduled library check, and when its results change, a notice reaches the notification feed.

## Fix a whole group of games with one click

When many games share one problem, the group gets one button instead of one button per game:

- **Grant access** gives a Flatpak emulator read-only access to one game folder, after you confirm. **Remove access** takes that back until you restart OpenBox.
- **Install** installs a missing emulator once for every game that was waiting on it.
- After a grant, only that group's games are checked again.

For a group of games with missing files, **Find moved files** opens the repair wizard limited to those games.

## Choose a RetroArch core for one game

When a game's RetroArch core is missing, **Choose core** lists the cores installed on this machine. The game launches with the one you pick, and **Use the default core** restores the definition's core. On Windows, the core you pick is now applied when the game launches. The core picker itself lists nothing there yet.

## Undo a Steam removal

Remove Steam games moves them to the Trash, with their position and playlists, and **Restore** brings them back. The Trash keeps 200 entries, so a removal moves only as many games as fit. The rest stay in your library, and the notice says how many.

## 18 more systems, ready to launch

Launch definitions now cover Sega Genesis and Mega Drive, Master System, Game Gear, Sega CD, PC Engine, Neo Geo Pocket, Atari 2600, 7800, Lynx and Jaguar, WonderSwan, Virtual Boy, C64, MSX, Amiga, Dreamcast, 3DS and MS-DOS. Most run through RetroArch and the rest through a standalone emulator. Each needs its emulator installed first, and for RetroArch systems the matching core too. The Launch Doctor names whichever is missing.

## Disc images ask which system they belong to

A `.bin`, `.cue` or `.iso` file that several systems share waits in the import wizard for you to choose its platform. OpenBox no longer guesses, so a PlayStation image is not imported as a Sega CD game.

## Definitions are signed

From this release, the definition pack is signed with the release key and published with each release. A pack written for a newer schema than your OpenBox reads is refused, and the error names the version it needs.

## A library that stays live and responsive

- **One live connection per window.** Busy windows keep getting updates instead of losing them to a connection limit.
- **Searching doesn't push the panels around.** Typing a search adds filter chips on their own line under the library title, and they scroll sideways if there are many. The drop zone and Play Insights no longer move down when the chips appear.
- **The sidebar search box is usable again.** The Title and Smart toggle sits under the search box instead of squeezing it, and its labels are translated.
- **The native window starts in your theme's colour.** A light theme no longer shows a dark window before the page appears.

## Fixed

- **Edits could land on the wrong game.** If an edit dialog stayed open while the library changed in another window, saving could write your changes onto a different game. OpenBox now checks the game's identity before it saves. If the game is gone, you get an error and nothing is written. Favorite, trash, screenshot, metadata, save and extra-launch actions use the same check.
- **Probes could hang.** A stuck Flatpak or D-Bus session counts the emulator as not installed after five seconds, instead of stalling the request.
- **Dates with a time zone were skipped.** A play date written with `Z` or an offset now counts in "played recently", idle rules and backup age. Those games were silently skipped before.
- **Bulk edit accepts only dates it can read.** A last-played value such as "yesterday" is refused, and the game keeps its old date.
- **Live updates stopped after hiding the window.** They return when the window is shown, and the scheduled-rescan notice retries if its connection drops.
- **Settings that failed to save stayed changed.** The cover grouping and the grid or list view go back to their old value with a message when a save fails. Startup storefront imports name the stores that failed.
- **Emulator health.** The BIOS button opens the BIOS folder where your system allows it. The missing-core and missing-firmware lines state the problem instead of offering buttons that did nothing.
- **Media manager.** **Retry failed** is back. It is enabled only when some downloads failed, and it retries only those games.
- **Windows per-game core.** A game's chosen RetroArch core was ignored on Windows and the default core always ran. It now applies there.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
