# OpenBox release notes: v1.16.1

OpenBox 1.17 finishes the launch check, adds launch definitions for 18 more systems, and keeps the library live
while it changes. It also carries the fixes from the 1.16.1 work: safer edits, probes that cannot hang, and dates
with a time zone. Full list: [CHANGELOG.md](CHANGELOG.md), compare
[v1.16.0...v1.16.1](https://github.com/vindeckyy/OpenBoxGL/compare/v1.16.0...v1.16.1).

## Launch readiness everywhere

- **Badges on the grid.** A game the last launch check found blocked shows **Won't launch**, and one with warnings
  shows **Needs attention**. The badges come from the last check and disappear when the library has changed since,
  so an old report never labels a game. Turn them off in Settings > Appearance if you don't want them.
- **The check keeps itself current.** It runs with your scheduled library check. When its results change, a notice
  reaches the notification feed.
- **One fix for a group.** When many games share one problem, such as one Flatpak emulator that cannot read one
  folder, or one emulator to install, the group has one button. After a grant, only that group's games are checked again.
- **Find moved files.** For a group of games with missing files, **Find moved files** opens the repair wizard limited
  to those games.
- **Choose a RetroArch core for one game.** When a game's core is missing, **Choose core** lists the cores installed
  here. The game launches with the one you pick. **Use the default core** restores the definition's core.
- **Flatpak folder access.** When a Flatpak emulator cannot read a game's folder, **Grant access** gives it read-only
  access to that one folder after you confirm. **Remove access** takes it back until you restart OpenBox.
- **Steam removals can be undone.** Remove Steam games moves them to the Trash, with their position and playlists, and
  Restore brings them back. The Trash keeps 200 entries, so a removal moves only as many games as fit; the rest stay in
  your library, and the notice says how many.

## Every system out of the box

- **18 more systems.** Sega Genesis and Mega Drive, Master System, Game Gear, Sega CD, PC Engine, Neo Geo Pocket,
  Atari 2600, 7800, Lynx and Jaguar, WonderSwan, Virtual Boy, C64, MSX, Amiga, Dreamcast, 3DS and MS-DOS. Most run
  through RetroArch and the rest through a standalone emulator. Each needs its emulator installed first, and for
  RetroArch systems the matching core too. The Launch Doctor names whichever is missing.
- **Disc images ask which system they belong to.** A `.bin`, `.cue` or `.iso` file that several systems share waits in
  the import wizard for you to choose its platform. OpenBox no longer guesses, so a PlayStation image is not imported
  as a Sega CD game.
- **Definitions are signed.** Definition packs are signed with the release key and published with each version. A
  pack written for a newer schema than your OpenBox reads is refused, with the version it needs.

## Always live and fast

- **One live connection per window.** Busy windows keep getting updates, instead of losing them to a connection limit.
- **Searching doesn't push the panels around.** Typing a search adds filter chips on their own line under the
  library title; they scroll sideways if there are many. The drop zone and Play Insights no longer move down when the
  chips appear. The sort and view menus sit on the lines below that line.
- **The sidebar search box is usable again.** The Title and Smart toggle now sits under the search box instead of
  squeezing it, and its labels are translated.
- **The native window starts in your theme's colour.** A light theme no longer shows a dark window before the page
  appears.

## Fixes from the 1.16.1 work

- **Your library is safer to edit.** If you had an edit dialog open while the library changed in another window, saving
  could write your changes onto a different game. OpenBox now checks the game's identity before it saves. If the game
  is gone, you get an error and nothing is written. Favorite, trash, screenshot, metadata, save and extra-launch
  actions use the same check.
- **Probes cannot hang.** A stuck Flatpak or D-Bus session counts the emulator as not installed after five seconds,
  instead of stalling the request.
- **Play dates with a time zone.** A date written with `Z` or an offset counts in `played recently`, idle rules and
  backup age. Before, those games were silently skipped.
- **Bulk edit rejects a bad last-played date.** A value OpenBox cannot read, such as "yesterday", is refused and the
  game keeps its old date.
- **Live updates come back.** After the window was hidden, updates stopped until a reload. They return when the window
  is shown, and the scheduled-rescan notice retries if its connection drops.
- **Setting saves say when they fail.** The cover grouping and the grid or list view go back to what they were when a
  save fails, with a message. Startup storefront imports name the stores that failed.
- **Emulator health.** The BIOS button opens the BIOS folder where your system allows it. The missing-core and
  missing-firmware lines state the problem, and no longer offer buttons that did nothing.
- **Media manager.** **Retry failed** is back. It is enabled only when some downloads failed, and it retries only
  those games.

The changelog lists the main areas, not every change.

## Upgrading

Upgrade to v1.16.1 from any 1.16.x release. The fixes above are not backported to older versions, and 1.15.x is no
longer supported.

## Known limits

- **RetroArch in Flatpak.** Checked on one machine with the Flathub build and the SNES core: a core in the sandbox's
  config folder loads, and OpenBox launches Flatpak RetroArch from there. Other cores and other Flatpak builds are
  not checked on a real install.
- **Some labels are still English.** Notifications are fully translated. Some dialog and button labels are not yet,
  and the notices the launch check and cloud sync send are English too.
- **Windows.** The Windows CI job builds the native host with MSVC against the WebView2 SDK and runs the Windows test
  suite; both pass on this release. Nobody has run the Windows app by hand on a Windows desktop yet.
- **The first signed definition pack** is published by the release workflow once the release key is in place.
