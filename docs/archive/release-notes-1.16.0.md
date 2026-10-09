# OpenBox release notes

Two questions every big library raises now have a one-click answer: **will my games actually launch?** and **what will restoring this backup do?** This release answers both, closes several security holes, and fixes a long list of places where OpenBox said one thing and did another. Everything below explains what changed, why it matters, and what you will see differently.

## Find every game that won't launch, before you try it

Until now, the only way to find out that a game wouldn't start was to click Play and watch it fail. OpenBox already had a Launch Doctor that could explain why one game was broken, but it only ever looked at one game at a time, after the fact. If you had just imported a few thousand ROMs, you had no way to know how many of them would actually work.

**How to use it:** open **Library health** and press **Check every game**. OpenBox runs the Launch Doctor over your whole library in the background. You can keep using the app while it works, and you can cancel it at any time.

**What you get back:** instead of a long list of individual failures, the problems are grouped by their cause. For example, rather than 412 separate "won't launch" entries, you see one line:

**RetroArch is not installed — 412 games**

That tells you the real problem in one sentence, and it tells you that fixing that one thing fixes all 412 games.

- **One click for a missing emulator.** When the cause is an emulator that isn't installed, the group has an **Install** button. Clicking it installs the emulator once for every game that was waiting on it.
- **A summary at the top.** You see how many games are ready to play, how many have warnings (for example, no cover art or no save folder set), and how many are blocked and will not start.
- **The games behind each cause.** Open any group to page through the games in it, then click a game to jump straight to it. Its details panel shows the full Launch Doctor result for that one game, so you can fix it there.
- **Fast, even on huge libraries.** The check looks up each emulator once rather than once per game, so a 20,000-game library is checked in seconds instead of the hour it would have taken game by game.
- **Read-only.** Running the check never changes your library. It only reports what it found.
- **A note when it's out of date.** If you add or remove games after a check, OpenBox tells you the results are from an older version of your library and suggests running it again.
- **Optional deep mode.** Tick the deep-mode box to also look inside zipped ROMs and check your BIOS files against their expected versions. It is off by default because it reads every file and takes longer.

See [Emulators and launching](https://openboxgl.github.io/guides/emulators-and-launching/).

## See what a restore will do before you do it

Restoring a backup replaces your current library with the one in the backup. Before this release, the only warning was a "Restore this backup?" prompt. You couldn't see what you were about to lose or get back, so restoring an old backup was a leap of faith.

**How it works now:** in **Backups**, choose **Preview changes** next to any backup. OpenBox compares the backup with your current library and shows you the difference before anything happens:

- **Games that would be removed.** These are in your library now but not in the backup, usually games you added after the backup was made.
- **Games that would come back.** These are in the backup but not in your library now, usually games you deleted since.
- **Games that would change.** For each one, you see exactly which fields differ, with your current value and the backup's value side by side (for example, a game you marked as Beaten since the backup would go back to Playing).
- **Your settings.** If the backup would also restore your settings, a line tells you so, because that can change things like where saves are stored and how your emulators are set up. If the backup leaves out saved account keys, it tells you your current ones will be kept.
- **True totals on long lists.** Big differences show the real count, such as "showing 200 of 1,340", so a long list never looks smaller than it is.

The **Restore** button only appears after a preview has loaded successfully. If the preview can't be loaded, for example because the backup file is damaged, you get a **Retry** button and no way to restore, so you can never restore a backup without having seen what it does. OpenBox still makes a safety copy of your current library before any restore.

See [Sessions, saves, and backups](https://openboxgl.github.io/guides/sessions-saves-and-backups/).

## Know when a game didn't start

If a game crashed or quit with an error, OpenBox used to show "Play time and history were saved", the same message as a normal session. A game that closed the moment it opened looked exactly like one you had played and quit.

Now OpenBox reads the game's exit code correctly:

- If the game exits with an error almost immediately, you see **Session failed** with the exit code and a pointer to check the launch command and the emulator install.
- If the game ran for a while and then exited with an error, you see **Session ended** with the exit code.
- A normal exit still shows the usual "Play time and history were saved".

The same information now reaches webhooks, which also get a separate flag when a session ended because it timed out.

## Imports you can trust

**Games the wizard isn't sure about now wait for you.** When the import wizard can't work out something about a game, such as which platform it belongs to, it marks it for review. Before, those games were quietly imported anyway, with whatever guess the wizard had made. Now they are left out until you choose what to do with them, and the final confirmation step shows how many are still waiting for a decision, so nothing is imported that the wizard said needed your attention.

**Your own playlists are left alone.** Multi-disc games often come with a `.m3u` playlist that lists the discs in order. If you had made your own playlist next to your disc images, an import could overwrite it with a generated one, and the game it pointed to could go missing from the results. OpenBox now never overwrites a playlist it didn't create, and the game is imported exactly once.

**LaunchBox and ES-DE imports ask first.** Applying a LaunchBox or ES-DE import rewrites your library from the source file. It now shows a confirmation with the exact numbers first: how many games will be added, how many will be merged into games you already have, and how many entries are in the plan. The review list before it also says when it is only showing the first 50 entries.

See [Importing](https://openboxgl.github.io/guides/library/importing/).

## Safer by default

Upgrade to v1.16.0 if you are on 1.15.x or older. These fixes are not backported to older versions, and 1.15.x is no longer supported.

- **Reading files through a crafted request.** A specially crafted request to OpenBox's local server could read any `.json` file on your computer, including the settings file where your metadata provider keys are stored. Every request that names a file is now checked so it can only reach the folder it is meant for.
- **Writing files through a high-score bundle.** Restoring a high-score bundle for a game with a tampered ROM name could write a file outside the high-score folder. The name is now cleaned and the destination is checked before anything is written.
- **Plugins reading your library.** Plugins run in a sandbox that hides your personal folders, but if you had moved your OpenBox data folder somewhere else with `OPENBOX_DATA_DIR`, sandboxed plugins could read your library and settings. The data folder is now hidden from plugins wherever it lives.
- **Unescaped text in Wrapped.** Game names in the Wrapped report were inserted into the page without escaping. This was blocked from running scripts by OpenBox's security policy, but it was one setting away from being exploitable. Names are now escaped, and an automatic check stops this pattern from coming back.
- **An outside package changing how your library is saved.** If a particular third-party Python package happened to be installed on your system, OpenBox used it to save your library, and it could write the file differently. OpenBox now uses only Python's own standard library, and an automatic check enforces that.

See [SECURITY.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/SECURITY.md).

## Fixed

- **Playtime landing on the wrong game.** If you deleted a game while it was still running and then restarted OpenBox, the hours from that session were added to whichever game happened to be last in your library. Now no game's playtime changes when the game it belongs to is gone.
- **Changes lost during a cloud sync.** An edit you made while a cloud sync was in progress could be overwritten when the sync finished. Your edit and the synced changes are now merged.
- **Dialogs closing abruptly.** Pressing Escape or clicking outside most dialogs closed them instantly, without their closing animation, and left your keyboard focus nowhere in particular. Every dialog now closes smoothly however you close it, and focus returns to the button that opened it.
- **Notifications erasing each other.** If a second notification arrived while one with an **Undo** button was showing, the Undo button could disappear before you clicked it. Notifications now stack, up to three at a time, each keeps its own button, and they pause while your mouse is over them or one is focused.
- **Half-finished emulator-definition updates.** If installing an emulator-definition update failed partway, you could be left with some definitions updated and others not. Updates are now prepared in full first and rolled back completely if anything fails.
- **Controller stops after alt-tab.** Switching away from OpenBox and back stopped gamepad input until you restarted. Your controller now keeps working.
- **Wrong game from the command palette.** Under some timing, the command palette could launch a different game from the one you had highlighted. It now launches the one you see.
- **Wrong extras on session cards.** The card for a running game could show another game's cover and extras. It now shows the right game.
- **Big Box video and music.** Closing Big Box could leave a game's preview video playing in a loop and the background music stuck at low volume for the rest of the session. Both now stop and reset when Big Box closes.
- **Searches.** Searching for just `-` emptied the library view, and quoted phrases could be split apart. Both are fixed. Very large searches that match more than 20,000 games now say they are showing only part of the results instead of silently cutting them off.
- **Endless spinners.** If the OpenBox server stopped responding, some screens spun forever. Requests now give up after 60 seconds and tell you.
- **Constellation view.** With reduced motion turned on, the Constellation view could freeze the whole page while it laid out the graph, and if loading failed its spinner never stopped. It now works in short steps that keep the page responsive, and shows an error if loading fails.
- **Arcade Room.** Cancelling a launch from the Arcade Room used to close the room anyway, with nothing launched. You now stay in the room.
- **Timeline, Time Machine, and Mastery.** Game covers in the timeline didn't load. In Time Machine, a quick double-click on **Load more** could stop it working until you reopened the window. Clicking a platform in the Mastery view did nothing; it now filters your library to that platform.
- **Museum-mode PIN prompt.** With the museum kiosk PIN turned on, the PIN prompt came back after every idle period, forever. After a failed or dismissed attempt it now waits longer before asking again, doubling each time.
- **Metadata matches.** Bulk-accepting metadata matches could fail with no message, so the button looked broken. It now tells you that nothing was accepted, and why.
