# OpenBox release notes

This release is about OpenBox doing what it says. It closes several security holes, stops two ways your data could be lost or misplaced, and adds two checks you can run before something goes wrong: will my games launch, and what will this restore change?

## Security fixes

Upgrade to v1.16.0 if you are on 1.15.x or older. None of these is backported.

- A crafted request could read any `.json` file on disk, including the settings file that holds your provider credentials
- Restoring a high-score bundle could write a file outside the high-score folder if a game's ROM name was crafted
- Sandboxed plugins could read your library when the data folder was moved with `OPENBOX_DATA_DIR`
- Game names in the Wrapped report reached the page without escaping
- An optional third-party JSON library, if it happened to be installed, could change how your library was saved. OpenBox now uses only the Python standard library at runtime, and a gate enforces it

See [SECURITY.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/SECURITY.md).

## Launch Readiness

**Library health** gains a **Check every game** button. It runs the Launch Doctor over your whole library in the background and groups what it finds by cause, so you see "RetroArch is not installed: 412 games" instead of 412 separate failures.

- One **Install** button per missing Flatpak emulator, for every game waiting on it
- Open a cause to page through its games; pick one to jump straight to it
- Ready, warning, and blocked totals at a glance, and a note when the library changed since the last check
- Never changes your library, and can be cancelled at any time
- A 20,000-game library is checked in seconds: each emulator is probed once, not once per game
- An optional deep mode also opens archives and checks BIOS hashes

See [Emulators and launching](https://openboxgl.github.io/guides/emulators-and-launching/).

## Restore Preview

Choosing a backup now shows what restoring it would do before you can restore it.

- Which games it removes, which it brings back, and which fields it overwrites, with the old and new value side by side
- True totals on long lists ("showing 200 of 1,340")
- A line when your settings would change too, including save paths and emulator profiles
- If the preview cannot load, you get a Retry button and no Restore button

See [Sessions, saves, and backups](https://openboxgl.github.io/guides/sessions-saves-and-backups/).

## A failed launch says so

A game that exits with an error now shows **Session failed** with its exit code. Before, every launch was reported as a success, including ones that died the moment they started.

## Imports that keep their word

- Candidates the import wizard marks for review wait for your decision instead of being imported anyway, and the Confirm step shows how many are still waiting
- A hand-made `.m3u` playlist next to your disc images is left exactly as it is, and its game is imported once
- Applying a LaunchBox or ES-DE import now asks for confirmation with the exact counts first

See [Importing](https://openboxgl.github.io/guides/library/importing/).

## Fixed

- Deleting a game while it was running, then restarting OpenBox, added its playtime to whichever game was last in the library. No game's playtime changes now.
- A change made while cloud sync was running could be dropped.
- Two background jobs of the same kind could both keep running when one replaced the other, and cancelling a finished job could cancel a different one.
- Pressing Escape or clicking outside most dialogs closed them without their exit animation and lost your keyboard position.
- A second notification could erase the Undo button on the first.
- A failed emulator-definition install could leave a half-written set of definitions behind.
- Gamepad input stopped working after alt-tab.
- The command palette could launch a different game from the one highlighted.
- Running-session cards could show another game's cover and extras.
- Big Box could keep a video looping, with the music turned down, after it closed.
- Searching for a bare `-` emptied the library, and a search matching more than 20,000 games now says it is showing part of the results.
- A request that never returns now times out after 60 seconds instead of leaving a spinner up.
- The Constellation view froze the page with reduced motion turned on, and its spinner never stopped if loading failed.
- Mood colours stopped updating after one cover failed to load.
- Bulk-accepting metadata matches could fail with no message.
- Cancelling a launch from the Arcade Room closed the room anyway.
- Timeline covers did not load, and Time Machine's **Load more** could stop working after a double click.
- Clicking a platform in the Mastery view did nothing; it now filters the library to that platform.
- The museum-mode PIN prompt came back after every idle period; it now waits longer after each dismissed or failed attempt.
- The What's New dialog showed an older release's highlights.
- Error messages in the emulator-definitions panel showed `&lt;` instead of `<`.
