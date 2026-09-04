# Documentation

The user-facing documentation site is a separate repository: `openboxgl.github.io` (Next.js static export, deployed to GitHub Pages at https://openboxgl.github.io). The marketing home page lives in `app/page.tsx` with its section components in `components/`, and user documentation lives under `content/docs/`.

The files under this `docs/` folder are engineering and planning documents that accompany the repo:

- `reliability.md` — edge case catalog of failure modes and expected behavior
- `native-host-contract.md` — contract between the page and the WebKitGTK native host
- `adr/` — Architectural Decision Records (ADR 0001 through 0036) covering native host, repository layout, parity shims, theme tokens, cache hierarchy, gate completion, lock ordering, state decomposition, namespace migration, setup preview, durable operations, emulator registry, artifact gates, SQLite read model, i18n system, gamescope presets, controller settings UI, BIOS SHA1 drift detection, backup diff API, smart collection chips, hash routing, ScreenScraper, library export, aarch64 artifacts, changed-line coverage, mood-match theming, library constellation, picker, wrapped timeline, mastery map, game night, SQLite read-model graduation, LaunchBox XML migration, Big Box video snaps, mounted-folder library sync, and manual shelf entries
- `development/` — development and handler conventions (`HANDLER_CONVENTIONS.md`, `PERF.md`)

Do not add user-facing markdown here. Edit the docs site repo instead.
