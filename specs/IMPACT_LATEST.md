# Impact assessment — e01 OpenBox bug sweep

## Target

The sweep observes the complete product, but production edits are permitted only in the narrow path of a confirmed P1/P2 defect. The primary inspection targets are:

- `web_app.py` — REST routing, authorization, validation, exception mapping, persistence orchestration, background jobs, and launch/session control.
- `index.html` — browser state, fetch handling, settings/import/launch/session/save/update journeys, dialog state, polling, and rendering.
- Shared domain modules — `openbox.py`, `env_config.py`, `parity_import.py`, `parity_premium.py`, `parity_gameyfin.py`, `saves.py`, `updates.py`, plugins, and integration adapters.
- Packaging contracts — Makefile, AppImage/Flatpak manifests, desktop/metainfo files, and `test_packaging.py`.

No target is pre-authorized for refactoring. A production file becomes an edit target only after a deterministic reproduction and root-cause gate.

## Zoom-out: purpose, callers, contracts

### `web_app.py`

**Purpose:** serve the browser client and translate authenticated HTTP requests into bounded domain operations and JSON responses.

**Callers:** `index.html`; local API consumers/deep links; `test_parity_api.py`; `test_sessions.py`; `test_auto_import.py`; `test_changelog_features.py`; launch scripts that start the server.

**Contracts:** protected routes authorize consistently; request bodies are validated; expected domain failures return stable 4xx JSON; unexpected failures do not drop connections; partial settings writes preserve omitted keys; long jobs return promptly and expose terminal status; user-data writes stay inside the selected data root.

### `index.html`

**Purpose:** present the complete web UI and coordinate browser-side state with roughly 100 `/api/*` references.

**Callers:** end users through `web_app.py`; screenshot/readme tooling; source-level parity and packaging tests.

**Contracts:** fetch failures are visible and recoverable; duplicate polling is prevented; dialogs do not leak stale state; settings collectors own non-overlapping fields; rendering tolerates missing/partial data; critical actions do not execute twice; responsive/accessibility behavior remains intact.

### Shared domain modules

**Purpose:** implement library persistence, imports, integrations, saves/backups, updates, metadata, plugins, emulators, and parity workflows outside the server boundary.

**Callers:** `web_app.py`, `openbox.py`, other domain modules, and matching `test_*.py` scripts. Higher fan-in modules include `env_config` and `openbox` (six known importers each), `parity_import` and `parity_premium` (five), and `parity_gameyfin`, `saves`, and `web_app` (four).

**Contracts:** deterministic pure transformations where possible; no secret leakage; safe path handling; atomic/non-destructive persistence; external failures are bounded; launch/plugin subprocess arguments preserve user intent without shell injection; import dedupe remains stable.

### Packaging surface

**Purpose:** install and distribute a self-contained Linux launcher.

**Callers:** Make/AppImage/Flatpak build tooling, desktop environments, update clients, and `test_packaging.py`.

**Contracts:** version, module lists, update metadata, desktop identity, metainfo, and legal files stay mutually consistent. Stale local AppImage contents must not be mistaken for current source behavior.

## Dependents and affected behavior

| Area | Representative dependents | Sweep risk |
|---|---|---|
| API/auth/settings | `index.html`, `test_parity_api.py`, `test_secrets.py` | Authorization bypass, secret exposure, dropped connections, unrelated settings reset |
| Imports/library | `web_app.py`, `openbox.py`, `parity_import.py`, storefront/integration modules | Duplicate/lost games, malformed inputs, blocking external calls |
| Launch/sessions/plugins | browser launch controls, emulator definitions, plugin hooks, session polling tests | command corruption, duplicate launch, stale session state, stuck hooks |
| Saves/backups | `saves.py`, `parity_saves.py`, web API/UI | data loss, traversal, unsafe overwrite, misleading success |
| Updates | `updates.py`, GET `/api/update`, browser update UI | NetworkError, digest mismatch, malformed release data |
| Packaging | Makefile, build scripts/manifests, desktop/metainfo, updater | missing runtime module, identity/version mismatch, unusable artifact |

## Affected stories

- **e01s01:** establishes the baseline and coverage/failure inventory without changing behavior.
- **e01s02:** probes shared API and domain boundaries; highest security and interface risk.
- **e01s03:** probes browser journeys and client/server contracts; overlaps the currently modified `index.html` only observationally until a bug is confirmed.
- **e01s04:** may modify any confirmed narrow defect path; requires a per-defect impact addendum before editing.
- **e01s05:** validates all dependents and packaging contracts after fixes.

## Test coverage

Existing evidence includes 26 passing standalone test modules through `./run_all_tests.sh` on 2026-07-26. Important mappings:

- `test_parity_api.py`, `test_sessions.py`: server routing and session behavior.
- `test_updates.py`: update parsing and verification.
- `test_secrets.py`, `test_env_config.py`: credential and environment boundaries.
- `test_auto_import.py`, `test_importers.py`, `test_parity_features.py`, `test_parity_playnite.py`: import and parity flows.
- `test_parity_gameyfin.py`, `test_parity_integrations.py`, `test_parity_storefront.py`: external integration adapters.
- `test_saves.py`, `test_cloud_sync.py`, `test_archives.py`: persistence, backup, and sync behavior.
- `test_plugins.py`: plugin contracts.
- `test_packaging.py`: distribution manifests, identity, versioning, legal policy, and update metadata.

### Gaps to target

- The browser has 103 named functions and roughly 100 API references but no dedicated browser end-to-end framework.
- Literal route inspection shows several endpoints without direct route-string assertions, including health, backups, saves discovery, favorites, RetroAchievements, and other dynamic route families; literal absence is a lead, not proof of missing behavioral coverage.
- Happy-path script tests may not prove malformed JSON, missing keys, wrong types, timeouts, repeated clicks/polls, aborted fetches, or filesystem failures.
- Live external services cannot be treated as deterministic test dependencies.
- Existing tests are script-style, so counts of `test_*` functions understate their assertion coverage.

## Working-tree collision risk

The sweep starts with pre-existing modifications in legal/docs/identity files plus `index.html` and `test_packaging.py`, and untracked `TRADEMARKS.md`. Those hunks are user-owned. Before any edit to an overlapping file, capture `git diff -- <file>`, make the smallest exact change, and verify the final diff preserves unrelated work.

## Risk: High

The sweep crosses a shared API/interface, filesystem mutation paths, subprocess boundaries, and packaging contracts. Risk is contained by isolated data roots, observation-first probes, root-cause gating, narrow patches, red-green regression tests, and a final whole-product gate.

## Recommended action

Proceed in ordered waves. Do not parallelize writes. Baseline first; then API and browser discovery; then process confirmed P1/P2 defects one at a time. If a root cause requires redesign, exceeds the fix budget, or touches an external dependency without deterministic local evidence, record and defer it rather than broadening e01.
