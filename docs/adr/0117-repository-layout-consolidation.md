# ADR 0117: Repository layout consolidation — native/, packaging/, scripts/

Status: Accepted (2026-11-22)

## Context

ADR 0002 reorganized the repository root and explicitly deferred three moves
"to later phases": native host sources, packaging metadata, and the remaining
root scripts. Two releases later the deferral had calcified: `native_host*.c/.m/.rc`
and `openbox.ico` sat beside `openbox.py` and `index.html`, the Flatpak manifest,
desktop entry, and AppStream metainfo shared the root with runtime Python modules,
and `build_appimage.sh` / `run_all_tests.sh` were the last shell scripts outside
`scripts/`.

ADR 0115/0116 made the deferral safe to execute: 3.0 ships the Rust
`openbox-server` and the Python tree is the parity/legacy harness, so every
consumer of the moved files is a build-time path (Makefile install rules, CI
compile and validation steps, `flatpak-builder`, release packaging) rather than
a runtime import.

## Decision

Move build-time files into folders; keep runtime-loaded files at root.

- `native/` — `native_host.c`, `native_host_mac.m`, `native_host_win.c`,
  `native_host_win.rc`, `openbox.ico`. Compiled binaries still land at the
  repo/share root (`native_host`, `native_host.exe`), so launchers and the
  `OPENBOX_*` path probes are unchanged.
- `packaging/` — `openbox.desktop`, `openbox.metainfo.xml`,
  `io.openbox.GameLauncher.yml`. Install destinations are unchanged
  (`applications/`, `metainfo/`, `share/openbox/`); the Flatpak manifest's
  `type: dir` sources now use `path: ..` because flatpak-builder resolves
  source paths relative to the manifest file (verified with
  `flatpak-builder --download-only`).
- `scripts/` — `build_appimage.sh` and `run_all_tests.sh` join the existing
  script collection; both resolve the repo root one level up.

Stay at root, deliberately:

- `web_app.py` — the Python fallback entry point, spawned by absolute path
  from the launchers, the native hosts, and the Rust strangler proxy
  (`strangler.rs` joins `web_app.py` to the app root).
- `index.html` — repo-root marker used by `openbox.sh` share-dir detection
  and served at `/` by `openbox-server`.
- `openbox.svg`, `openbox-release.pub` — read from the share root at runtime
  (`static_files.rs`, native host icon lookup, `include_bytes!`/`root.join`
  in `openbox-integrations`).
- Launchers (`openbox.sh`, `openbox-native.sh`, `openbox.cmd`, `openbox.ps1`,
  `openbox-native.ps1`) — user-facing entry points referenced by docs,
  desktop files, and muscle memory.

## Amendment (same day): the flat `*.py` modules move to `app/`

The "keep flat modules at root" call in ADR 0002 was about avoiding a package
rewrite, not about root placement being load-bearing. Bare-name imports
(`import plugins`, `from saves import ...`) resolve through `sys.path`, so the
modules move to `app/` — a plain directory, not a package — and `app/` joins
`sys.path` wherever Python runs: `web_app.py` inserts it before its local
imports, `run_all_tests.sh` and `run_windows_tests.py` prepend it to
`PYTHONPATH`, and each test/script that already inserted the repo root gained a
sibling `... / "app"` insert. `runtime_modules.txt` entries become `app/*.py`,
so installs land at `share/openbox/app/` and `web_app.py`'s bootstrap finds
them there unchanged. `plugin_runner_v2.py` moves with the rest; the Rust
plugin host's `locate_runner_v2` joins `app/plugin_runner_v2.py`, and the
bubblewrap sandbox still bind-mounts the runner's parent directory, so the
in-sandbox path is unchanged.

`__file__`-relative lookups that pointed at share-root siblings
(`plugins/catalog.json`, `routes/`, `openbox-release.pub`, `openbox.svg`,
`webapp_state.ROOT`, `updates.py`'s install root) resolve one level up via
`parent.parent`; `stock_themes.py` already probed both layouts.
`web_app.py`, `openbox.py`, and `updates.py` carry explicit `sys.path`
bootstraps so direct `python app/<module>.py` invocation still resolves
`pkg/`/`handlers/`.
`assets/OpenBoxLogo.svg` and root `openbox.svg` are not duplicates:
`scripts/render_branding.sh` generates the latter from the former.

## Consequences

- `AGENTS.md` documents the Rust workspace, the new folders, and the
  `OPENBOX_BACKEND=python` escape hatch; it previously described only the
  Python-era layout.
- ADRs and archived release notes keep their historical paths; they are
  point-in-time records, not living documentation.
- `scripts/check_native_paths.py`, `check_version_sync.py`,
  `validate_flatpak_manifest.py`, `test_packaging.py`, `test_i18n.py`,
  `test_ci_gates.py`, `test_native_host.py`, `test_native_background.py`,
  and `test_frontend_contract.py` resolve the new locations.
