# Documentation

The user-facing documentation site is a separate repository: `openboxgl.github.io` (Next.js static export, deployed to GitHub Pages at https://openboxgl.github.io). The marketing home page lives in `app/page.tsx` with its section components in `components/`, and user documentation lives under `content/docs/`.

The files under this `docs/` folder are engineering and planning documents that accompany the repo:

- `reliability.md` — edge case catalog of failure modes and expected behavior
- `native-host-contract.md` — contract between the page and the WebKitGTK native host
- `adr/0001-native-host.md` — accepted ADR for the native host decision
- `development/DESIGN.md`, `development/PERF.md`, `development/PRODUCT.md`, `development/COVERAGE.md`, `development/PLAN.md` — design, performance, product, coverage, and planning notes (local-only; kept out of git via `.gitignore`)

Do not add user-facing markdown here. Edit the docs site repo instead.
