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

- All root `*.py` modules — `runtime_modules.txt` mirrors them into the share
  dir and they are imported by bare name; moving them is an architectural
  change, not a tidy-up (ADR 0002).
- `index.html` — repo-root marker used by `openbox.sh` share-dir detection
  and served at `/` by `openbox-server`.
- `openbox.svg`, `openbox-release.pub`, `openbox.ico`'s installed copies —
  read from the share root at runtime (`static_files.rs`, native host icon
  lookup, `include_bytes!`/`root.join` in `openbox-integrations`).
- Launchers (`openbox.sh`, `openbox-native.sh`, `openbox.cmd`, `openbox.ps1`,
  `openbox-native.ps1`) — user-facing entry points referenced by docs,
  desktop files, and muscle memory.

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
