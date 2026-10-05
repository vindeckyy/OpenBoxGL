# OpenBox release notes

Two questions every big library raises now have a one-click answer: **will my games actually launch?** and **what will restoring this backup do?** This release answers both, closes several security holes, and fixes the places where OpenBox said one thing and did another.

## Find every game that won't launch, before you try it

Imported a few thousand ROMs? Open **Library health** and press **Check every game**. OpenBox tests your whole library in the background and sorts what it finds into plain groups, for example:

**RetroArch is not installed — 412 games** → one **Install** button fixes all of them

- When an emulator is missing, one click installs it for every game waiting on it
- See at a glance how many games are ready, need attention, or won't start
- Open any group to see its games, then jump straight to one
- Checks 20,000 games in seconds and never changes your library
- Turn on deep mode to also look inside zipped ROMs and check your BIOS files

See [Emulators and launching](https://openboxgl.github.io/guides/emulators-and-launching/).

## See what a restore will do before you do it

Pick a backup and OpenBox shows you exactly what would happen to your library:

- Which games would disappear, which would come back, and which would change, with the old and new value side by side
- Whether your settings would change too, including save paths and emulator profiles
- Real totals, even on long lists ("showing 200 of 1,340")

The **Restore** button only appears once you have seen the preview. No more restoring blind.

See [Sessions, saves, and backups](https://openboxgl.github.io/guides/sessions-saves-and-backups/).

## Know when a game didn't start

If a game crashes or quits with an error, OpenBox now tells you: **Session failed**, with the exit code, so you know where to look. Before, every launch was reported as a success, even one that closed the moment it opened.

## Imports you can trust

- Games the import wizard is unsure about wait for your call instead of being imported anyway, and the last step shows how many are still waiting
- Your own `.m3u` playlists for multi-disc games are left exactly as you made them
- Importing from LaunchBox or ES-DE now shows the exact numbers and asks before it changes your library

See [Importing](https://openboxgl.github.io/guides/library/importing/).

## Safer by default

Upgrade to v1.16.0 if you are on 1.15.x or older; these fixes are not backported.

- A crafted request could read files on your computer, including the settings file where your account keys live
- A tampered high-score bundle could write files outside its folder
- Plugins could read your library if you had moved your data folder
- Game names in the Wrapped report were not escaped
- OpenBox now runs on Python's standard library alone, so an unrelated package on your system can no longer change how your library is saved

See [SECURITY.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/SECURITY.md).

## Fixed

- Deleting a game while it was running could move its playtime onto a different game. It can't now.
- A change made during a cloud sync could be lost.
- Escape and clicking outside a dialog now close it smoothly and put your keyboard focus back where it was.
- A new notification no longer wipes out the Undo button on the one before it.
- A failed emulator-definition update now rolls back completely instead of leaving a half-finished set.
- Your controller keeps working after you alt-tab away and back.
- The command palette launches the game you highlighted, not a different one.
- Session cards show the right game's cover and extras.
- Big Box no longer leaves a video looping, with the music turned down, after you close it.
- Searching for just `-` no longer empties the library, and very large searches say when they are showing only part of the results.
- A server that stops responding now gives up after 60 seconds instead of spinning forever.
- The Constellation view no longer freezes the page when reduced motion is on, and stops its spinner if loading fails.
- Cancelling a launch from the Arcade Room keeps you in the room.
- Timeline covers load, Time Machine's **Load more** keeps working, and clicking a platform in the Mastery view filters your library to it.
- The museum-mode PIN prompt backs off after a failed or dismissed attempt instead of returning every idle period.
- Bulk-accepting metadata matches tells you when nothing was accepted, and why.
