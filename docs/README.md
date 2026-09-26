# Documentation

The user-facing documentation site is a separate repository: `openboxgl.github.io` (Next.js static export, deployed to GitHub Pages at https://openboxgl.github.io). The marketing home page lives in `app/page.tsx` with its section components in `components/`, and user documentation lives under `content/docs/`.

The files under this `docs/` folder are engineering, process, and release documents that accompany the repo:

## Reference

- `reliability.md` — edge case catalog of failure modes and expected behavior
- `native-host-contract.md` — contract between the page and the native hosts (WebKitGTK on Linux, WebView2 on Windows)
- `PARITY.md` — feature parity tracker against LaunchBox and Playnite
- `api-v2.md` — v2 REST API reference, generated from `routes.py`, `contracts.py`, and `routes/registry.py`; regenerate with `python3 scripts/gen_api_docs.py --v2`, do not edit by hand
- `plugin-api.md` — plugin API v1 reference: manifest, hooks, sandbox, trust, catalog (frozen per ADR 0050)
- `development/` — development conventions: `DESIGN.md` (design system: colors, typography, components), `HANDLER_CONVENTIONS.md`, `PERF.md` (performance budgets and measurements)
- `adr/` — Architectural Decision Records (ADR 0001 through 0060) covering native host, repository layout, parity shims, theme tokens, cache hierarchy, gate completion, lock ordering, state decomposition, namespace migration, setup preview, durable operations, emulator registry, artifact gates, SQLite read model, i18n system, gamescope presets, controller settings UI, BIOS SHA1 drift detection, backup diff API, smart collection chips, hash routing, ScreenScraper, library export, aarch64 artifacts, changed-line coverage, mood-match theming, library constellation, picker, wrapped timeline, mastery map, game night, SQLite read-model graduation, LaunchBox XML migration, Big Box video snaps, mounted-folder library sync, manual shelf entries, causal sync safety, reviewable LaunchBox source identity, the docs archive layout, perf-gate warm-up with trimmed p95, the Library Time Machine journal contract, the deterministic query grammar contract, the Every Second Counts local-first surfaces, the Living Library additions (smart collections, Game Story, per-game launch options, scheduled backups, SQLite auto-enable, CSP framing gate), the Windows port, Windows installer rollback, the plugin API freeze, library repair and duplicate-merge contracts, Game DNA search, the library health score, the backlog progress enum, artwork hygiene, the plugin trust model, the plugin events hook, dated-notes migration, ROM-hash match auto-scrape, and the feature-regression ratchets

## Gates

`make check` runs the full gate. The feature-regression ratchets (ADR 0060)
can also be run alone with `make ratchets`, and their frozen contracts are
regenerated with `make contracts` after an intentional surface change.

- `scripts/contracts/` — the frozen baselines. Every one is a **ratchet**: a
  surface may grow freely, and may only shrink by moving an entry to a
  `retired` ledger with a reason. Each baseline is itself ratcheted against
  git, so deleting an entry to hide a removal also fails.
- `check_routes_contract.py` — the full HTTP route surface (the v1 contract
  covers only 61 routes).
- `check_settings_contract.py` — the destructive `KNOWN_SETTINGS` allowlist;
  retiring a user-data key requires a named data-preservation migration.
- `check_emulator_defs.py` — every definition carries `native_exe_windows`
  (ADR 0048) and the other required keys.
- `check_frontend_modules.py` — the ES module graph: no dangling imports, no
  orphaned modules.
- `check_reliability_catalog.py` — every `Tested` row in `reliability.md` names
  a test that exists.
- `check_clock_coupling.py` — no unmarked test pins a calendar date against a
  rolling `*_DAYS` window.


## Release

- `CHANGELOG.md` — versioned history of all notable changes (Keep a Changelog format)
- `RELEASE_NOTES.md` — release notes for the current version
- `flathub-checklist.md` — remaining steps toward Flathub store submission

The current shipped release is **v1.14.0**. The planning and execution records
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

- `archive/` — superseded planning documents and historical release notes kept for traceability: `NEXT_UPDATE_PLAN.md` (the executed 1.9.0→1.10.0 plan), `NEXT_UPDATE_PLAN-1.11.md` (the 1.11.0 planning record), `NEXT_UPDATE_PLAN-1.12.md` (the executed 1.12.0 plan), `NEXT_UPDATE_EXECUTION.md` (the executed 1.9.0→1.10.0 ledger), `NEXT_UPDATE_EXECUTION-1.11.md` (the executed 1.11.0 ledger), `HANDOFF-WINDOWS-PORT.md` (the Windows port handoff, shipped under ADR 0048), `release-notes-1.7.2.md`, `release-notes-1.8.0.md`, `release-notes-1.11.0.md`

Do not add user-facing markdown here. Edit the docs site repo instead.
