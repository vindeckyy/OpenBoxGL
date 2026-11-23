# OpenBox - Agent Conventions

Read this before writing code. A convention that lives only in a human's head is one prompt away from being ignored.

## Where things go
- `crates/` - the Rust workspace (ADR 0115): 15 crates producing the `openbox-server` binary that 3.0 ships. `cargo build --release -p openbox-cli` produces it; `make build-rust` / `make rust-check` are the gates.
- `app/` - the flat runtime module surface: `runtime_modules.txt` mirrors these into `share/openbox/app/` and they are imported by bare name, resolved by putting `app/` on `sys.path` (`web_app.py` bootstraps it; tests and scripts insert it alongside the repo root). `app/openbox.py` is the shared core library for data paths, state, and launch helpers.
- `web_app.py` stays at root as the Python fallback entry point (spawned by path from launchers, native hosts, and the Rust strangler). `index.html`, `openbox.svg`, and `openbox-release.pub` also stay at root: the server, native hosts, and update channel read them from the share root.
- Launch paths: `openbox.sh` / `openbox-native.sh` on Linux and macOS, `openbox.cmd` / `openbox.ps1` / `openbox-native.ps1` on Windows, `OpenBox.app` (built by `scripts/build_macos_app.sh`) on macOS. The launchers resolve the Rust `openbox-server` by default; `OPENBOX_BACKEND=python` falls back to `web_app.py` for side-by-side soak testing (ADR 0115). The native window sources live in `native/` (`native_host_mac.m` on macOS). Platform seams live in `pkg/platform_compat.py` (ADR 0048, ADR 0085). Feature gates that hide platform-specific controls come from its `platform_features()` contract (ADR 0086).
- `handlers/` - HTTP handlers, one module per domain or feature surface (library, imports, media, party, picker, etc.). Shared helpers live in `_shared.py`.
- `pkg/parity/` - parity_* modules (clear names, cohesive cluster) plus the shared `launch_tokens.py` helper. New parity code goes there. Parity modules are imported as `pkg.parity.*`; the flat `parity_*` bridge was removed in 2.0 and `scripts/check_flat_imports.py` keeps flat imports from returning.
- `static/` - Frontend JavaScript modules, the search worker, and app.css.
- `themes/` - theme CSS files, each overrides `:root` tokens only.
- `emulator_defs/` - YAML definitions.
- `native/` - native host sources for the three platforms plus the Windows icon resource (`openbox.ico`, `native_host_win.rc`). Compiled binaries still land at the repo/share root.
- `packaging/` - install-time metadata: `openbox.desktop`, `openbox.metainfo.xml`, and the Flatpak manifest `io.openbox.GameLauncher.yml` (its `path:` sources resolve relative to `packaging/`, so they point at `..`).
- `tests/` - standalone `test_*.py` scripts run via `./scripts/run_all_tests.sh` (`python3 -B file.py` style). Tests are feature/module-scoped, not a strict 1:1 mirror; every new runtime module still needs a covering test file.
- `scripts/` - check_tests.py, check_tokens.py, check_api_contract.py, build_appimage.sh, run_all_tests.sh, etc.
- `docs/` - all markdown except README.md and LICENSE. ADR in `docs/adr/`. Superseded plans and old release notes go in `docs/archive/`, never deleted.
- `build/`, `native_host`, `target/`, `.venv-dev/`, `.coverage` - generated, never commit.

New integration goes in `pkg/parity/`, `handlers/`, `app/` (flat runtime modules), or `crates/` as appropriate, never loose at root. New runtime module must be added to `runtime_modules.txt`; new Rust code goes in the matching `crates/openbox-*` member.

## Design system
- Every color, spacing, and typography value comes from a token in `static/app.css` `:root` (`--brand`, `--focus`, `--surface-card`, `--font-heading`, etc.).
- No raw hex in component rules outside `:root`. A new visual value means a new token plus its entry in each theme file.
- Token names are the theme contract. Themes override `:root` and mostly nothing else.
- `scripts/check_tokens.py` enforces this with a ratcheting baseline (ratcheted to 0, originally 625).

## Non-negotiables
- Runtime stays dependency-free. `pyproject.toml` dev deps are not installed in AppImage.
- Rust workspace gates: `make rust-check` runs `cargo fmt --check`, `clippy -D warnings`, `cargo deny check`, and `cargo test --workspace`. Keep it green alongside `make check`.
- The v1 API surface is retired (ADR 0091, ADR 0099): off by default, `OPENBOX_ENABLE_V1_API=1` bridges it through 2.0.x, and the frozen 61-route list is archived at `docs/archive/v1_contracts.json`. The public route contract is `api_contracts.json` + `scripts/check_api_contract.py`. Drift fails the gate.
- **The whole feature surface is ratcheted** (ADR 0060, `scripts/contracts/`).
  Routes, `KNOWN_SETTINGS`, emulator definitions, frontend modules, and the
  reliability catalog may **grow freely** and may only **shrink** by moving
  the entry to a `retired` ledger with a reason. Each baseline is itself
  ratcheted against git, so deleting an entry to hide a removal also fails.
  Run `make contracts` after an intentional change; `make ratchets` runs the
  gates alone.
- Coverage floors go up, never down. `COVERAGE_FLOOR` and `WEB_APP_FLOOR` in `scripts/check_tests.py` are ratcheted.
- New runtime module must be added to `runtime_modules.txt` (enforced for root `*.py`, `handlers/`, `pkg/state/`, `pkg/parity/`, `routes/`) and have a `test_*.py` (convention, not gate-enforced).
- Every emulator definition must carry `native_exe_windows` and the other keys
  in `scripts/check_emulator_defs.py::REQUIRED_KEYS`.
- A test that pins a calendar date while exercising a rolling `*_DAYS` window
  needs a `# clock-coupled: <mitigation>` marker plus an entry in
  `scripts/contracts/clock_coupling.json`.

## Tests
- One `test_*.py` per module, standalone-script style; Rust crates carry `#[cfg(test)]` unit tests and `crates/openbox-parity-tests` holds cross-crate parity checks.
- Run all Python tests with `./scripts/run_all_tests.sh` (walks `tests/` preferentially; root `test_*.py` only run when `tests/` has none). Single file: `make test-one TEST=tests/test_saves.py`.
- Full Python gate with `make check` → `scripts/check_tests.py`: ruff, runtime_modules, dependencies, api_contract, version_sync, frontend, i18n, csp, the contract ratchets (`make ratchets`), py_compile, tests under coverage, coverage floors (total + web_app), changed-line, new-module, tokens.

## Before opening a PR
- `make check` and `make rust-check` must pass locally.

## Conventions
- Commit format: conventional, scope is module.
- When adding a UI component, add a token if needed and update all themes.
