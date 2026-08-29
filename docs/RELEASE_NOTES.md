# OpenBox v1.7.1

Hey, thanks for trying OpenBox. 1.7.1 is a polish release that builds on 1.7.0. The big pieces from 1.7.0 are still there, but they now feel finished when you have a large library. Setup actually helps you finish, the library stays fast at 20,000 games, and you can finally see what you have been playing.

We kept everything local and offline. No accounts, no telemetry, and your library is still just a JSON file on disk.

## What is new

### Library Setup Center

The setup flow walks you through eight clear steps from welcome to completion. You see a preview of what will be added, merged or skipped before anything is written, and if the library changes while a preview is open it will tell you it is stale instead of applying old decisions. Finished imports are tagged so you can filter to just what you just added, and you can reopen setup any time from the top bar.

### Activity Center

Long running work now lives in `operations.json` so it survives restarts. The top bar shows live counts for active and attention-needed jobs, and the drawer streams progress over SSE. You can cancel, retry or resume depending on the job type, and the old `/api/jobs` endpoints still work for scripts.

### Launch Doctor

Before you hit Play, OpenBox checks if the game can actually launch. It looks at the path, which emulator adapter matches, whether the Flatpak or native binary is there, BIOS and firmware hints, and your launch arguments. The batch check in setup step 5 does this for your whole library at once, and both the normal launch and Big Box use the same check so you get a clear message instead of a silent failure.

### Play Insights

New in 1.7.1 is a local insights view. It reads your existing `history` and `games` and shows a 366 day heatmap, your current and longest streak, top platforms and genres, and how the last 30 days compares to the 30 before that. It is just two new endpoints, `GET /api/v2/insights/summary` and `/heatmap`, and everything is computed on your machine. A 20,000 entry history builds the heatmap in about 15 ms.

### Performance and scale

We support 20,000 games. The library grid now virtualizes with spacers and `IntersectionObserver`, search can run in a worker with a main-thread fallback, and the facet and write paths are bounded and coalesced. The existing 10k and 20k gates are still enforced in CI.

### Packaging

As before we ship x86_64 artifacts from Ubuntu 22.04: a signed AppImage with zsync and SBOM, and a Flatpak on runtime 25.08. This is still a release-gated bundle, not a Flathub store listing, and there is no telemetry in the artifacts.

## If you are starting from empty

1. Open OpenBox, click Set up library in the empty state or top bar.
2. Pick where your ROMs live, preview the scan, review the few items that need a choice, check emulator readiness and run the metadata review.
3. Commit, watch Activity for progress, then launch from the grid or Big Box.

## What we intentionally did not do

No database rewrite, no ARM64 builds, no translations, no Flathub submission, no library schema 7, and no v1 route changes. Those are for later.

## How we tested this

`make check` is green, 76 test files pass, and the AppImage and Flatpak were built from the exact tagged commit. The full changelog is at https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.0...v1.7.1.

Thanks for the bug reports and ideas that made this polish possible. If something still feels rough, open an issue and we will fix it.
