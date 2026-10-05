# OpenBox 1.16.0 — Make it true

This release makes OpenBox do what it says. It closes a set of security holes, stops two ways the app could lose or misplace your data, and corrects four things the 1.15.0 notes claimed that the code did not do. It also adds two features that answer questions you could not ask before: *will my games launch?* and *what will this restore change?* Everything still runs 100% locally — no account, no cloud, no telemetry.

---

## Security

Upgrade if you run 1.15.x or older. Details are in [SECURITY.md](SECURITY.md); in short:

- A crafted request could read any `.json` file on disk, including the settings file that holds your provider credentials.
- A crafted game name could make a high-score restore write a file outside its folder.
- Sandboxed plugins could read your library when the data folder was moved with `OPENBOX_DATA_DIR`.
- The Wrapped report put game names into the page without escaping them.
- An optional third-party JSON library, if installed, could change how your library was saved. OpenBox now uses only the Python standard library at runtime, and a gate enforces it.

## What's New

- **Launch Readiness.** Library health now has a *Check every game* button. It runs the Launch Doctor on your whole library in the background and groups what it finds by cause: not 400 broken games, but "RetroArch is not installed — 400 games", with one *Install* button for the lot. Open a cause to see its games; pick one to jump to it. The check never changes your library, and on a 20,000-game library it takes seconds, not the hour it would have taken game by game.
- **Restore Preview.** Choosing a backup now shows what restoring it would do before you can restore it: which games it removes, which it brings back, which fields it overwrites, and whether your settings would change. Long lists show their true size ("showing 200 of 1,340"). If the preview can't be loaded, there is a Retry button and no Restore button.

## Corrections to 1.15.0

The 1.15.0 notes described four behaviours that had only partly shipped. They work now, and each one has a test that fails if it stops working ([ADR 0064](adr/0064-changelog-truth-contract.md)).

- **"Every dialog plays an exit."** Only when closed with a button. Escape and a click outside the dialog closed most dialogs instantly and lost your keyboard position. All 29 dialogs now animate out and return focus however they are closed.
- **The toast queue.** There was one notification slot, and a second message could erase the first one's Undo button. Notifications are now a real queue: up to three stack, each keeps its own button, and a toast pauses while you hover or focus it.
- **Emulator-definition updates were atomic.** A failed install could leave a half-written set of definitions. Installs now stage everything first and roll back completely on failure.
- **The reduced-motion check.** The test that guarded it could pass for code that ignored the setting. It now requires every animated module to use the shared motion setting.

## Behaviour you will notice

None of these is a regression, but each looks like one if you don't know why:

- **A failed launch now says so.** A game that exits with an error, or immediately, shows "Session failed" with the exit code. Before, every launch was reported as a success.
- **The import wizard no longer imports what it said needed review.** Candidates marked for review (for example, an unknown platform) used to be imported anyway. They now wait for your decision, and the Confirm step shows how many are waiting.
- **Session cards show the right game.** The running-session card could show another game's cover and extras.
- **Your own `.m3u` playlists are left alone.** If a folder already has a hand-made playlist beside its disc images, the import keeps it exactly as it is instead of overwriting it.

## Fixed

- **Playtime credited to the wrong game.** Deleting a game while it was running, then restarting OpenBox, added its hours to whichever game was last in the library. Now no game's playtime changes.
- **Cloud sync could drop a change** made while a sync was running; it now merges inside the same write.
- **Two background jobs of the same kind** could both keep running when one replaced the other; the old one is now cancelled and recorded.
- **Gamepad input stopped working after alt-tab.**
- **The command palette could launch a different game** from the one highlighted.
- **Big Box could keep a video snap looping**, with the music turned down, after you closed it.
- **Searching with a bare `-`** emptied the library; quoted phrases now stay together.
- **A search matching more than 20,000 games** now says it is showing part of the results.
- **A request that never returns** now times out after 60 seconds instead of leaving a spinner forever.
- **The constellation view froze the page** with reduced motion turned on, and its spinner never stopped if loading failed.
- **The mood colours could stop updating** after one image failed to load.
- **Bulk-accepting metadata matches** could fail silently; it now says nothing was accepted and why.
- **Cancelling a launch from the Arcade Room** closed the room anyway.
- **Timeline covers** didn't load, **Time Machine's Load more** could stop working after a double click, and **mastery bars** did nothing when clicked (a platform row now filters the library to that platform).
- **The museum-mode PIN prompt** reappeared after every idle period; it now waits longer after each dismissed or failed attempt.
- **Error messages in the emulator-definitions panel** showed `&lt;` instead of `<`.
- **Changing the Game DNA search mode** could fail silently and revert on restart; it now rolls back and says so.
- **Library writes with no changes** no longer touch the disk, and the SQLite read model can no longer be overwritten by an older rebuild.

## Not in this release

Each item below was held on purpose. The first ten are the 1.15.0 carry-forward list with what happened to each; the rest were cut from 1.16.0 under its own plan.

| Deferred | Status in 1.16.0 |
|---|---|
| **Deck Builder for Game Night** | Not in 1.16.0, a fourth deferral. This release was spent on correctness. It needs a decision to lead the next release or be cancelled, not another quiet slip. |
| **Search-worker facets** | Not built. Still conditional on a long-task probe showing jank during a facet recompute. |
| **Presence, saves / play-state sync** | Not started. Still needs an ADR and a privacy review. |
| **Windows storefront importers** (GOG / EA / Ubisoft) | Not in 1.16.0. Still the largest Windows parity gap and the first candidate for 1.17. |
| **Per-platform setup checklist, "finish it" shelf, Constellation viewpoints / path / PNG** | Not in 1.16.0. Feature work. |
| **Kiosk PIN backoff** | **Shipped in part.** The museum-mode prompt now backs off on the client after a failed or dismissed attempt. The server-side check is still stateless. |
| **QR phone remote, `price_paid` cost/hour, 50k performance gate** | Not in 1.16.0, for the same reasons as before. |
| **Wayland fractional scaling, Steam Deck manual passes** | Not in 1.16.0. They need a physical device. |
| **GTK minimum window size** | Not in 1.16.0. Needs a native-host rebuild. |
| **macOS** | Out of scope per `SUPPORT.md`. |
| **Launch Readiness on the game cards** | Cut. The check reports per cause in Library health, and the details pane already runs the Doctor for the selected game; a badge on every card waits for 1.17. |
| **One-click fixes beyond "Install emulator"** | Cut. Fixes that would edit many games (setting a platform, relinking paths) need a dry-run preview and undo like the health fixes. Until then those causes link to the games instead. |
| **Layout shift when a search returns nothing** | Deferred (S55). The filter bar shifts once (0.055 CLS, inside the 0.1 "good" threshold). Cosmetic. |

---

Full changelog: https://github.com/vindeckyy/OpenBoxGL/compare/v1.15.0...v1.16.0
