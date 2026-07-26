# OpenBox technical architecture

## Product shape

OpenBox is a Linux-first game-library launcher with two local user interfaces sharing JSON-backed state:

- `openbox.py`: native Tk interface and common data-root definitions.
- `web_app.py`: standard-library HTTP server exposing a token-authenticated `/api/*` surface.
- `index.html`: single-page browser client that calls the REST API.
- Domain modules: imports, emulators, metadata, saves, updates, plugins, archives, achievements, cloud sync, and `parity_*` slices.
- Distribution: AppImage, Flatpak manifest, desktop entry, metainfo, launch scripts, and files enumerated by packaging manifests/tests.

## Runtime and dependencies

- Python 3, primarily standard library.
- HTML/CSS/vanilla JavaScript browser client.
- Shell scripts and Make for build/install automation.
- YAML configuration for emulator definitions and Flatpak packaging.
- No configured lint command; syntax and source checks use Python/stdlib plus repository tests.
- No new dependency is proposed for e01. `[OK]` Python stdlib and existing browser automation are sufficient.

## Data and trust boundaries

1. Browser → `web_app.py`: bearer/token authorization, JSON validation, HTTP status and error-body contracts.
2. `web_app.py` → domain modules: exceptions must become bounded JSON responses rather than dropped connections.
3. Domain modules → filesystem: library/settings/backups/plugins under the resolved data root; atomicity and path safety matter.
4. Import/integration modules → external programs/services: absent credentials, unavailable programs, malformed responses, and timeouts are normal failure modes.
5. Launcher/plugin hooks → subprocesses: arguments, lifecycle, timeout, and untrusted plugin output are security/reliability boundaries.
6. Source tree → packages: module/file lists must stay synchronized across Makefile, AppImage, Flatpak, and packaging tests.

## Core contracts to preserve

- `/api/*` protected routes enforce authorization consistently and return JSON errors without dropping the connection.
- `public_settings()` never exposes secret values.
- `save_settings` merges omitted keys; partial POSTs cannot reset unrelated settings.
- Gameyfin install is asynchronous, reaches a terminal state, and browser polling is bounded.
- Session polling does not overlap and stale state cannot overwrite newer state.
- User data remains under the configured OpenBox data root and is never written into the repository.
- `openbox.py` and `web_app.py` are not run concurrently against the same data root.
- A new Python module must be listed consistently in Makefile, AppImage build, Flatpak manifest, and `test_packaging.py`.
- Every behavior change includes a regression test.

## Test architecture

- `./run_all_tests.sh` runs 26 standalone `test_*.py` modules with fail-fast shell semantics.
- API/session coverage: `test_parity_api.py`, `test_sessions.py`, `test_updates.py`, `test_secrets.py`.
- Import/domain coverage: `test_auto_import.py`, `test_importers.py`, `test_parity_features.py`, `test_parity_playnite.py`, and integration-specific modules.
- Packaging coverage: `test_packaging.py` plus AppImage build when required.
- UI behavior is largely source-checked or manually exercised; e01 adds browser evidence rather than a new UI framework.

## Current risk hotspots

- `web_app.py` and `index.html` form a large shared interface with roughly 100 UI API references.
- Recent churn is concentrated in `updates.py`, `index.html`, `web_app.py`, packaging manifests, and `test_parity_api.py`.
- High fan-in modules include `env_config`, `openbox`, `parity_import`, `parity_premium`, `parity_gameyfin`, `saves`, and `web_app`.
- External integrations do not all have stable/public APIs; offline and malformed-response behavior needs explicit probing.
- Pre-existing legal/trademark working-tree changes overlap `index.html` and `test_packaging.py`; bug fixes must preserve unrelated hunks.
