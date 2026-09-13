# Documentation

The user-facing documentation site is a separate repository: `openboxgl.github.io` (Next.js static export, deployed to GitHub Pages at https://openboxgl.github.io). The marketing home page lives in `app/page.tsx` with its section components in `components/`, and user documentation lives under `content/docs/`.

The files under this `docs/` folder are engineering, process, and release documents that accompany the repo:

## Reference

- `reliability.md` — edge case catalog of failure modes and expected behavior
- `native-host-contract.md` — contract between the page and the WebKitGTK native host
- `PARITY.md` — feature parity tracker against LaunchBox and Playnite
- `development/` — development conventions: `DESIGN.md` (architecture overview), `HANDLER_CONVENTIONS.md`, `PERF.md` (performance budgets and measurements)
- `adr/` — Architectural Decision Records (ADR 0001 through 0046) covering native host, repository layout, parity shims, theme tokens, cache hierarchy, gate completion, lock ordering, state decomposition, namespace migration, setup preview, durable operations, emulator registry, artifact gates, SQLite read model, i18n system, gamescope presets, controller settings UI, BIOS SHA1 drift detection, backup diff API, smart collection chips, hash routing, ScreenScraper, library export, aarch64 artifacts, changed-line coverage, mood-match theming, library constellation, picker, wrapped timeline, mastery map, game night, SQLite read-model graduation, LaunchBox XML migration, Big Box video snaps, mounted-folder library sync, manual shelf entries, causal sync safety, reviewable LaunchBox source identity, the docs archive layout, perf-gate warm-up with trimmed p95, the Library Time Machine journal contract, the deterministic query grammar contract, and the Every Second Counts local-first surfaces

## Release

- `CHANGELOG.md` — versioned history of all notable changes (Keep a Changelog format)
- `RELEASE_NOTES.md` — release notes for the current version
- `flathub-checklist.md` — remaining steps toward Flathub store submission

The current shipped release is **v1.11.0**. The planning and execution records
below are historical archives, not an unfinished active roadmap.

## Process and policies

- `CONTRIBUTING.md` — development setup, test/gate workflow, coding conventions
- `TRIAGE.md` — issue triage process, severity labels, response targets
- `SUPPORT.md` — supported platforms, Python versions, data locations
- `SECURITY.md` — security policy and vulnerability reporting
- `CODE_OF_CONDUCT.md` — contributor covenant
- `DISCLAIMER.md` — legal disclaimer
- `TRADEMARKS.md` — trademark and naming policy

## Archive

- `archive/` — superseded planning documents and historical release notes kept for traceability: `NEXT_UPDATE_PLAN.md` (the executed 1.9.0→1.10.0 plan), `NEXT_UPDATE_PLAN-1.11.md` (the 1.11.0 planning record), `NEXT_UPDATE_EXECUTION.md` (the executed 1.9.0→1.10.0 ledger), `NEXT_UPDATE_EXECUTION-1.11.md` (the executed 1.11.0 ledger), `release-notes-1.7.2.md`, `release-notes-1.8.0.md`

Do not add user-facing markdown here. Edit the docs site repo instead.
