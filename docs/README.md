# Documentation

The user-facing documentation site is a separate repository: `openboxgl.github.io` (Next.js static export, deployed to GitHub Pages at https://openboxgl.github.io). The marketing home page lives in `app/page.tsx` with its section components in `components/`, and user documentation lives under `content/docs/`.

The files under this `docs/` folder are engineering and planning documents that accompany the repo:

- `reliability.md` — edge case catalog of failure modes and expected behavior
- `native-host-contract.md` — contract between the page and the WebKitGTK native host
- `adr/` — Architectural Decision Records (ADR 0001 through 0013) covering native host, repository layout, parity shims, theme tokens, cache hierarchy, gate completion, lock ordering, state decomposition, namespace migration, setup preview, durable operations, emulator registry, and artifact gates
- `development/` — development, design, performance, and handler conventions (`DESIGN.md`, `PERF.md`, `HANDLER_CONVENTIONS.md`, `PLAN-archived-v0.9.md`)

Do not add user-facing markdown here. Edit the docs site repo instead.
